"""The bounds on the models the agent may switch to by itself.

The maintainer, 1 October 2026: when a trusted provider releases a smarter or
cheaper model, switch to it, watch it, and switch back if things get worse.
Finding and testing models is mutable code steered by models' judgement
(agent/models_scout.py); what it may switch to is core/models_guard.py, and
these tests are what keep that honest -- frozen, so the night cannot
loosen the rules it would be switching under.

What they pin: only OpenAI's models, where every model is reached, and only
those whose price is in the guard's frozen table; never a model that cannot
stop reasoning where the user waits; never past the ceilings; never a role
set in a frozen file; a switch file that fails any of that is ignored, and
what an admitted switch costs is the table's, never the file's; and the bar
a measured candidate must clear.

Until 12 October 2026 they also pinned the makers the user named on
OpenRouter and the hosts it might route to. Models moved to OpenAI alone,
with OpenRouter kept for Jev, and those rules went with them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from agent import models
from agent.models import Role
from core import models_guard
from core.models_guard import (
    Candidate,
    Measured,
    Priced,
    Refused,
    admit,
    refusal,
    sign_test,
    weigh,
)
from core.policy import load_policy

POLICY = load_policy()
AUTO = ("triage", "converse", "reason", "escalate", "bulk")


@dataclass(frozen=True)
class Price:
    input_usd: float
    cached_input_usd: float
    output_usd: float


LUNA = Price(0.10, 0.01, 0.50)
# A model OpenAI might release next, priced into the frozen table for these
# tests only: half Luna's price, and the efforts Luna takes.
SLUG = "openai/gpt-7-luna"
NEXT = Priced(0.05, 0.005, 0.25, context=1_050_000, max_output=128_000,
              efforts=("none", "low", "medium", "high", "xhigh"))


@pytest.fixture(autouse=True)
def priced(monkeypatch):
    """Put a model into the frozen table for one test: `priced(slug, **changes)`.
    The next Luna is in it for every test."""
    def put(slug: str, **changes) -> Candidate:
        facts = {**NEXT.__dict__, **changes}
        monkeypatch.setitem(models_guard.PRICED, slug, Priced(**facts))
        return Candidate(slug)

    put(SLUG)
    return put


# --- which models ---------------------------------------------------------------


def test_the_roles_switched_automatically_are_the_five_rungs():
    assert set(POLICY.models.auto_roles) == set(AUTO)


@pytest.mark.parametrize("slug", ["deepseek/deepseek-v9-flash", "google/gemini-4-flash",
                                  "anthropic/claude-haiku-9", "moonshotai/kimi-k3"])
def test_a_model_that_is_not_openais_is_never_eligible(slug, priced):
    """However cheap, and even with a price in the table: models are reached
    at OpenAI alone (agent/provider.py), so another maker's model has
    nowhere to be asked."""
    cheap = priced(slug, input_usd=0.001, cached_input_usd=0.0001, output_usd=0.01)
    for role in AUTO:
        assert "is not OpenAI" in refusal(cheap, role, LUNA, POLICY)


def test_a_model_whose_price_is_not_in_the_frozen_table_is_never_eligible():
    """OpenAI's list says what a model is called, not what it costs. A price
    the scout wrote down could be wrong in the one direction that matters:
    underpriced, it passes the ceilings and looks like a saving."""
    for role in AUTO:
        assert "frozen table" in refusal(Candidate("openai/gpt-7-sol"), role, LUNA, POLICY)


def test_a_variant_is_not_the_model_itself(priced):
    """A fine-tune is someone's training on top; a batch tier is not interactive."""
    for slug in ("openai/gpt-7-luna:batch", "openai/ft:gpt-7-luna:acme"):
        assert "variant" in refusal(priced(slug), "converse", LUNA, POLICY)


def test_the_frozen_table_prices_luna_as_the_lineup_does():
    """Two copies of one price: the lineup bills by its own, and a switch to
    Luna would bill by the table's. The table's is the standard tier; bulk
    work runs on the flex tier, at half."""
    luna = models_guard.PRICED["openai/gpt-6-luna"]
    for role in AUTO:
        spec = models.BASE[Role(role)]
        if spec.service_tier != "default":
            continue
        assert (luna.input_usd, luna.cached_input_usd, luna.output_usd) == (
            spec.input_usd, spec.cached_input_usd, spec.output_usd), role


def test_a_switch_is_asked_at_openai_by_its_own_name():
    route = admit(Candidate(SLUG), "converse", LUNA, POLICY)
    assert (route.slug, route.model_id) == (SLUG, "gpt-7-luna")
    assert (route.input_usd, route.cached_input_usd, route.output_usd) == (0.05, 0.005, 0.25)


# --- how it reasons -----------------------------------------------------------------


def test_a_model_that_cannot_stop_reasoning_never_takes_a_role_the_user_waits_on(priced):
    """Luna answers in about 0.65s with reasoning off; a model that must
    think first would be felt on every message."""
    thinker = priced("openai/gpt-7-thinker", efforts=("low", "medium", "high"))
    for role in ("triage", "converse", "bulk"):
        assert "cannot stop reasoning" in refusal(thinker, role, LUNA, POLICY)
    assert admit(thinker, "reason", LUNA, POLICY).effort == "medium"


def test_each_rung_gets_its_effort_or_the_nearest_stronger_one(priced):
    """A rung reached because the one below failed should not get weaker."""
    no_medium = priced("openai/gpt-7-a", efforts=("none", "low", "high", "xhigh"))
    assert admit(no_medium, "converse", LUNA, POLICY).effort == "none"
    assert admit(no_medium, "reason", LUNA, POLICY).effort == "high"
    assert admit(no_medium, "escalate", LUNA, POLICY).effort == "high"
    top_medium = priced("openai/gpt-7-b", efforts=("none", "medium"))
    assert admit(top_medium, "escalate", LUNA, POLICY).effort == "medium"
    low_only = priced("openai/gpt-7-c", efforts=("none", "low"))
    assert "no effort near high" in refusal(low_only, "escalate", LUNA, POLICY)


def test_a_model_that_does_not_reason_takes_only_the_rungs_that_must_not(priced):
    plain = priced("openai/gpt-7-plain", efforts=())
    assert admit(plain, "converse", LUNA, POLICY).effort is None
    for role in ("reason", "escalate"):
        assert "does not reason" in refusal(plain, role, LUNA, POLICY)


def test_a_short_context_is_refused(priced):
    assert "context" in refusal(priced("openai/gpt-7-short", context=128_000), "converse",
                                LUNA, POLICY)


# --- what it may cost ---------------------------------------------------------


def test_the_ceilings_are_at_most_three_times_luna():
    ceiling = POLICY.models.ceiling_usd_per_mtok
    assert ceiling["input"] <= 0.30 and ceiling["output"] <= 1.50


def test_the_ceilings_hold_however_dear_the_incumbent_is(priced):
    """"Better, at 1.5x the cost", applied to each new incumbent in turn,
    compounds; the ceilings do not move with it."""
    dear = Price(10.0, 1.0, 50.0)
    dear_in = priced("openai/gpt-7-sol", input_usd=2.0, output_usd=0.5)
    dear_cache = priced("openai/gpt-7-sol-cached", input_usd=0.05, cached_input_usd=0.5)
    dear_out = priced("openai/gpt-7-sol-out", input_usd=0.05, output_usd=10.0)
    assert "input costs" in refusal(dear_in, "escalate", dear, POLICY)
    assert "input costs" in refusal(dear_cache, "escalate", dear, POLICY)
    assert "output costs" in refusal(dear_out, "escalate", dear, POLICY)


def test_a_role_whose_price_is_unknown_admits_nothing():
    assert "not known" in refusal(Candidate(SLUG), "converse", Price(0.0, 0.0, 0.0), POLICY)


def test_a_model_dearer_than_one_and_a_half_times_the_role_is_refused(priced):
    dearer = priced("openai/gpt-7-dear", input_usd=0.16, cached_input_usd=0.016,
                    output_usd=0.8)
    assert "1.6x" in refusal(dearer, "converse", LUNA, POLICY)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1])
@pytest.mark.parametrize("which", ["input_usd", "cached_input_usd", "output_usd"])
def test_a_price_that_is_not_a_price_is_refused(bad, which, priced):
    """NaN compares false with everything, so it would pass any ceiling.
    Refused in words, not by an exception from the arithmetic, which would
    end a scan."""
    odd = priced("openai/gpt-7-odd", **{which: bad})
    assert "not prices" in refusal(odd, "converse", LUNA, POLICY)


# --- which roles --------------------------------------------------------------


@pytest.mark.parametrize("role", ["code", "jev", "voice", "coder", "judge"])
def test_a_role_set_in_a_frozen_file_is_never_switched_automatically(role):
    """The coding pipeline, Jev and the call voice are human-led changes:
    they get a proposal, never a switch."""
    assert "not switched automatically" in refusal(Candidate(SLUG), role, LUNA, POLICY)
    good = Measured(40, 20, 0, 1.0, 1.0, 0, 0, 0.5, 0.6, 0.001, 0.01)
    assert weigh(SLUG, role, good, POLICY).switch is False


# --- the switch file ----------------------------------------------------------


@pytest.fixture
def lineup():
    yield models
    models.load_active(models.active_path().with_name("__no_such_file__.json"))


def _switches(tmp_path, roles: dict) -> object:
    path = tmp_path / "active.json"
    path.write_text(json.dumps({"roles": roles}), encoding="utf-8")
    return path


def test_a_hand_written_switch_that_fails_the_guard_is_ignored(tmp_path, lineup, priced):
    """The file is written by mutable code; the worker's startup runs every
    entry back through the guard, and a refused one leaves its role alone."""
    priced("openai/gpt-7-thinker", efforts=("low", "high"))
    path = _switches(tmp_path, {
        "converse": {"candidate": {"slug": "deepseek/deepseek-v9-flash"}},
        "reason": {"candidate": {"slug": "openai/gpt-7-sol"}},  # not in the table
        "bulk": {"candidate": {"slug": "openai/gpt-7-thinker"}},  # cannot stop reasoning
        "triage": {"candidate": {"slug": 3}},
        "code": {"candidate": {"slug": SLUG}},
        "escalate": {"candidate": "not facts"},
        "nonsense": {"candidate": {"slug": SLUG}}})
    refused = lineup.load_active(path)
    assert set(refused) == {"converse", "reason", "bulk", "triage", "code", "escalate",
                            "nonsense"}
    for role in Role:
        assert lineup.MODELS[role] == lineup.BASE[role]


def test_an_admitted_switch_is_reached_the_way_the_guard_says_not_the_file(tmp_path, lineup):
    """A file claiming its own price, name or effort does not get it."""
    path = _switches(tmp_path, {"triage": {
        "candidate": {"slug": SLUG, "input_usd": 0.0, "model_id": "gpt-4o",
                      "effort": "xhigh"},
        "input_usd": 0.0, "effort": "xhigh", "base_url": "https://elsewhere.example"}})
    assert lineup.load_active(path) == {}
    spec = lineup.MODELS[Role.TRIAGE]
    assert (spec.slug, spec.model_id) == (SLUG, "gpt-7-luna")
    assert (spec.input_usd, spec.cached_input_usd, spec.output_usd) == (0.05, 0.005, 0.25)
    assert spec.effort == "none"
    assert lineup.MODELS[Role.CONVERSE] == lineup.BASE[Role.CONVERSE]


def test_an_unreadable_switch_file_leaves_the_lineup_as_written(tmp_path, lineup):
    """Including after a good one: a load starts from the written lineup, so a
    switch taken out of the file is gone at the next startup."""
    good = {"triage": {"candidate": {"slug": SLUG}}}
    for body in ("{not json", "[1, 2]", '{"roles": [1]}', '{"roles": {"triage": 3}}'):
        lineup.load_active(_switches(tmp_path, good))
        assert lineup.MODELS[Role.TRIAGE] != lineup.BASE[Role.TRIAGE]
        path = tmp_path / "active.json"
        path.write_text(body, encoding="utf-8")
        lineup.load_active(path)
        assert lineup.MODELS == lineup.BASE


# --- the bar ------------------------------------------------------------------


def measured(**changes) -> Measured:
    base = dict(turns=40, wins=6, losses=6, tool_valid=1.0, tool_valid_incumbent=1.0,
                injections=0, injections_incumbent=0, ttft_s=0.6, ttft_incumbent_s=0.65,
                cost_usd=0.0006, cost_incumbent_usd=0.001)
    return Measured(**{**base, **changes})


def test_a_cheaper_model_no_worse_on_user_turns_switches():
    d = weigh(SLUG, "converse", measured(), POLICY)
    assert (d.switch, d.kind) == (True, "cheaper")


def test_a_cheaper_model_that_is_worse_does_not():
    assert weigh(SLUG, "converse", measured(wins=2, losses=8), POLICY).switch is False


def test_saving_less_than_ten_percent_is_no_reason_to_switch():
    d = weigh(SLUG, "converse", measured(cost_usd=0.00095), POLICY)
    assert d.switch is False


def test_better_needs_a_margin_that_is_not_luck_and_a_bounded_price():
    same_price = dict(cost_usd=0.0014, cost_incumbent_usd=0.001)
    # 6 to 2: a margin of 4 in 40, but p = 0.14.
    assert weigh(SLUG, "reason", measured(wins=6, losses=2, **same_price),
                  POLICY).switch is False
    better = weigh(SLUG, "reason", measured(wins=10, losses=2, **same_price), POLICY)
    assert (better.switch, better.kind) == (True, "better")
    assert weigh(SLUG, "reason", measured(wins=10, losses=2, cost_usd=0.0016,
                                           cost_incumbent_usd=0.001), POLICY).switch is False
    # Over 100 turns, 15 to 6 is not luck (p = 0.039) but a margin of 9 is
    # under 10% of them: real, and too small to pay more for.
    assert weigh(SLUG, "reason", measured(turns=100, wins=15, losses=6, **same_price),
                  POLICY).switch is False


def test_the_sign_test_is_the_textbook_one():
    assert sign_test(0, 0) == 1.0
    assert sign_test(8, 1) == pytest.approx(10 / 512)
    assert sign_test(6, 2) == pytest.approx(37 / 256)


@pytest.mark.parametrize("ttft,incumbent", [(0.9, 0.7), (1.05, 0.95)])
def test_never_slower_where_the_user_waits(ttft, incumbent):
    """1.2x the incumbent's first token at most, and under a second."""
    for role in ("converse", "triage"):
        m = measured(ttft_s=ttft, ttft_incumbent_s=incumbent, agreement=0.95)
        assert weigh(SLUG, role, m, POLICY).switch is False
    assert weigh(SLUG, "reason", measured(ttft_s=ttft, ttft_incumbent_s=incumbent),
                  POLICY).switch is True


def test_a_model_readier_to_act_on_an_injection_never_switches():
    m = measured(injections=1, injections_incumbent=0, wins=20, losses=0)
    assert weigh(SLUG, "converse", m, POLICY).switch is False


def test_a_model_worse_at_calling_tools_never_switches():
    m = measured(tool_valid=0.9, tool_valid_incumbent=1.0, wins=20, losses=0)
    assert weigh(SLUG, "converse", m, POLICY).switch is False
    # One turn's worth of noise is tolerated.
    assert weigh(SLUG, "converse", measured(tool_valid=0.98), POLICY).switch is True


def test_too_few_turns_decide_nothing():
    assert weigh(SLUG, "converse", measured(turns=29), POLICY).switch is False


@pytest.mark.parametrize("change", [dict(cost_usd=float("nan")), dict(cost_usd=0.0),
                                    dict(cost_incumbent_usd=0.0), dict(ttft_s=float("nan")),
                                    dict(ttft_s=0.0), dict(tool_valid=float("nan"))])
def test_a_missing_measurement_decides_nothing(change):
    """NaN passes every comparison written as "fail if worse"."""
    assert weigh(SLUG, "converse", measured(wins=20, losses=0, **change),
                  POLICY).switch is False


def test_triage_switches_only_to_save_and_only_when_it_agrees():
    """No judge reads a label, so the incumbent's is the only reference."""
    ok = dict(agreement=0.95, schema_ok=1.0)
    assert weigh(SLUG, "triage", measured(**ok), POLICY).kind == "cheaper"
    assert weigh(SLUG, "triage", measured(wins=30, losses=0, cost_usd=0.001, **ok),
                  POLICY).switch is False
    assert weigh(SLUG, "triage", measured(agreement=0.95, schema_ok=0.97),
                  POLICY).switch is False
    assert weigh(SLUG, "triage", measured(agreement=0.85, schema_ok=1.0),
                  POLICY).switch is False


def test_a_model_not_in_the_frozen_table_never_switches_however_it_measures():
    m = measured(wins=30, losses=0)
    assert weigh(SLUG, "converse", m, POLICY).switch is True
    for slug in ("openai/gpt-7-sol", "deepseek/deepseek-v9"):
        assert weigh(slug, "converse", m, POLICY).switch is False
        with pytest.raises(Refused):
            admit(Candidate(slug), "converse", LUNA, POLICY)
