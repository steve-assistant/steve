"""Switching, putting back, the day's watch, the shortlist and the night's
round do what they say.

Who may switch, and what puts a switch back, are pinned in
tests/gate/test_model_switching.py; these pin the rest: what is written and
recorded, what the user is told, what the week's round tries and how cheaply a
losing candidate is stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import time

import pytest

from agent import models, models_eval, models_scout, models_switch
from agent.models import BASE, Role
from agent.night import scouting
from agent.night.report import NightReport, digest
from agent.worker.answering import REVERT_MODELS_RE
from core import canary, db
from core import models_guard as guard
from core.models_guard import BY_HAND, Candidate, Decision, Priced
from core.policy import load_policy

POLICY = load_policy()
ON = dataclasses.replace(POLICY, models=dataclasses.replace(POLICY.models, auto_switch=True))
SLUG = "openai/gpt-7-luna"
YES = Decision(True, "cheaper", "38% cheaper")


@pytest.fixture(autouse=True)
def priced(monkeypatch):
    """Put a model into the guard's frozen table: `priced(slug, inp, out)`.
    GPT-7 Luna, at half Luna's price, is in it for every test."""
    def put(slug: str, inp: float = 0.05, out: float = 0.25) -> None:
        monkeypatch.setitem(guard.PRICED, slug, Priced(
            inp, inp / 10, out, context=1_050_000, max_output=128_000,
            efforts=("none", "low", "medium", "high")))

    put(SLUG)
    return put


def _facts(slug=SLUG) -> Candidate:
    return Candidate(slug)


@pytest.fixture
def conn():
    c = db.open_store()
    yield c
    c.close()


def _candidate(conn, facts: Candidate, released="2026-10-01", roles=None) -> None:
    roles = roles or {r: None for r in POLICY.models.auto_roles}
    conn.execute("INSERT INTO model_candidates (slug, released, facts, roles, eligible)"
                 " VALUES (?, ?, ?, ?, ?)",
                 (facts.slug, released, json.dumps(facts.to_dict()), json.dumps(roles),
                  sum(v is None for v in roles.values())))
    conn.commit()


def _trial(conn, slug=SLUG, role="converse", decision=YES) -> int:
    with conn:
        return int(conn.execute(
            "INSERT INTO model_evals (slug, role, incumbent, decision, finished_at)"
            " VALUES (?, ?, 'openai/gpt-6-luna', ?, datetime('now'))",
            (slug, role, json.dumps(dataclasses.asdict(decision)))).lastrowid)


# --- switching and putting back ------------------------------------------------------


def test_a_switch_is_written_recorded_and_told_and_applied_on_asking(conn):
    _candidate(conn, _facts())
    trial = _trial(conn)
    said = models_switch.switch(conn, SLUG, Role.CONVERSE, by=BY_HAND)
    assert "everyday replies" in said and "REVERT MODELS" in said and "38% cheaper" in said
    entry = models_switch.read_switches()["converse"]
    assert (entry["candidate"]["slug"], entry["eval_id"], entry["by"]) == (SLUG, trial, "user")
    row = conn.execute("SELECT * FROM model_switches").fetchone()
    assert (row["kind"], row["from_slug"], row["to_slug"], row["outcome"]) == (
        "switch", BASE[Role.CONVERSE].slug, SLUG, None)
    assert json.loads(row["baseline"])["turns"] == 0
    # Not in force until applied: the caller's job.
    assert models.MODELS[Role.CONVERSE] == BASE[Role.CONVERSE]
    from agent import provider

    provider.model_for(Role.CONVERSE)  # a client cached for the old model
    assert models_switch.apply() == {}
    assert models.MODELS[Role.CONVERSE].slug == SLUG
    # And the next turn's client is the new model's, not the cached one.
    assert provider.model_for(Role.CONVERSE).model_name == "gpt-7-luna"


def test_by_hand_a_switch_needs_no_trial_but_still_the_bounds(conn):
    _candidate(conn, _facts())
    models_switch.switch(conn, SLUG, Role.REASON, by=BY_HAND)
    models_switch.revert(conn, by=BY_HAND, why="t")
    _candidate(conn, _facts("deepseek/deepseek-v9"))
    with pytest.raises(models_switch.SwitchRefused, match="is not OpenAI"):
        models_switch.switch(conn, "deepseek/deepseek-v9", Role.REASON, by=BY_HAND)
    _candidate(conn, _facts("openai/gpt-7-sol"))
    with pytest.raises(models_switch.SwitchRefused, match="frozen table"):
        models_switch.switch(conn, "openai/gpt-7-sol", Role.REASON, by=BY_HAND)
    with pytest.raises(models_switch.SwitchRefused, match="last scan"):
        models_switch.switch(conn, "openai/never-seen", Role.REASON, by=BY_HAND)


def test_putting_one_role_back_leaves_the_others(conn):
    _candidate(conn, _facts())
    models_switch.switch(conn, SLUG, Role.CONVERSE, by=BY_HAND)
    roles = models_switch.read_switches()
    roles["reason"] = roles["converse"]
    models_switch._write_switches(roles)
    assert models_switch.revert(conn, ["reason"], by=BY_HAND, why="t") == ["reason"]
    assert set(models_switch.read_switches()) == {"converse"}
    assert models_switch.revert(conn, by=BY_HAND, why="t") == ["converse"]
    assert not models.active_path().exists()
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM model_switches ORDER BY id")]
    assert kinds == ["switch", "revert", "revert"]


def test_a_quiet_day_keeps_the_switch_and_says_so(conn):
    _candidate(conn, _facts())
    day_ago = time.time() - 25 * 3600
    models_switch.switch(conn, SLUG, Role.CONVERSE, by=BY_HAND, now=day_ago)
    said = models_switch.tick(conn)
    assert said and said[0].startswith(f"Kept {SLUG} for everyday replies")
    assert conn.execute("SELECT outcome FROM model_switches").fetchone()[0] == "kept"
    assert models_switch.tick(conn) == []  # nothing left to watch


def test_a_watch_inside_its_day_with_nothing_wrong_says_nothing(conn):
    _candidate(conn, _facts())
    models_switch.switch(conn, SLUG, Role.CONVERSE, by=BY_HAND, now=time.time() - 3600)
    assert models_switch.tick(conn) == []
    assert models_switch.read_switches()


def test_a_switch_taken_away_by_hand_ends_its_watch(conn):
    _candidate(conn, _facts())
    models_switch.switch(conn, SLUG, Role.CONVERSE, by=BY_HAND)
    models.active_path().unlink()
    assert models_switch.tick(conn) == []
    assert models_switch.watching(conn) is None


def test_the_old_models_calls_are_its_own_effort_only(conn):
    """Luna at "none" is the everyday role; its "high" calls are another rung."""
    stamp = canary.sqltime(time.time() - 60)
    rows = [("openai/gpt-6-luna", "none", 800, 0.001), ("openai/gpt-6-luna", "none", 1000, 0.001),
            ("openai/gpt-6-luna", "high", 9000, 0.02)]
    conn.executemany("INSERT INTO usage_events (model, effort, latency_ms, cost_usd, created_at)"
                     " VALUES (?, ?, ?, ?, ?)", [(*r, stamp) for r in rows])
    since, until = canary.sqltime(time.time() - 3600), canary.sqltime(time.time())
    calls = models_switch._calls(conn, "openai/gpt-6-luna", since, until, "none")
    assert (calls.count, calls.median_s, calls.usd_each) == (2, 0.9, 0.001)
    assert models_switch._calls(conn, "x/none", since, until).count == 0


@pytest.mark.parametrize("text,match", [
    ("REVERT MODELS", True), ("revert models", True), ("Revert model.", True),
    ("  revert   models!  ", True), ("revert models please", False),
    ("can you revert models", False), ("revert", False),
])
def test_revert_models_is_two_words_the_user_can_send_from_memory(text, match):
    assert bool(REVERT_MODELS_RE.match(text)) is match


def test_the_command_line_will_not_switch_past_a_trial_that_said_no(conn, capsys):
    _candidate(conn, _facts())
    _trial(conn, decision=Decision(False, "", "lost 20 of 40"))
    args = argparse.Namespace(command="switch", slug=SLUG, role="converse", anyway=False)
    assert models_scout._by_hand(conn, args) == 2
    assert "lost 20 of 40" in capsys.readouterr().err
    assert models_switch.read_switches() == {}


# --- the shortlist ---------------------------------------------------------------


def test_the_shortlist_is_what_the_bounds_admit_newest_first_and_not_tried_lately(
        conn, priced):
    luna = BASE[Role.CONVERSE].slug
    _candidate(conn, _facts(luna), released="2026-09-25")
    _candidate(conn, _facts("openai/gpt-7-old"), released="2026-03-01")
    priced("openai/gpt-7-old")
    _candidate(conn, _facts("openai/gpt-7-new"), released="2026-10-10")
    priced("openai/gpt-7-new", inp=0.06, out=0.3)
    # Eligible when scanned, but its price has gone up since: the bounds
    # are asked again, and they say no.
    _candidate(conn, _facts("openai/gpt-7-dear"), released="2026-10-11")
    priced("openai/gpt-7-dear", inp=0.16, out=0.8)
    # No price in the table: scanned as unable to play anything.
    _candidate(conn, _facts("openai/gpt-7-sol"), released="2026-10-11",
               roles={r: "no price" for r in POLICY.models.auto_roles})
    _candidate(conn, _facts("openai/gpt-7-tried"), released="2026-10-12")
    priced("openai/gpt-7-tried")
    for role in ("converse", "reason", "escalate"):
        _trial(conn, "openai/gpt-7-tried", role)

    picks = models_scout.shortlist(conn, limit=20)

    # Everyday replies first: most of what the user says goes there.
    assert [(p["slug"], p["role"]) for p in picks[:2]] == [
        ("openai/gpt-7-new", Role.CONVERSE), ("openai/gpt-7-old", Role.CONVERSE)]
    assert picks[0]["why"] == "released 2026-10-10, at 0.60x the cost"
    assert not {p["slug"] for p in picks} & {"openai/gpt-7-dear", "openai/gpt-7-sol",
                                             "openai/gpt-7-tried", luna}
    assert len(models_scout.shortlist(conn)) == models_scout.TRIALS_A_WEEK


# --- stopping a losing trial early -------------------------------------------------


def _row(outcome, mine_ok=True, theirs_ok=True):
    return {"outcome": outcome, "step": [mine_ok, theirs_ok]}


def test_a_trial_plainly_losing_stops_and_one_close_to_even_does_not():
    assert models_eval.hopeless([_row(-1)] * 5 + [_row(0)] * 5) is not None
    assert models_eval.hopeless([_row(-1)] * 5 + [_row(1)] + [_row(0)] * 4) is None
    assert models_eval.hopeless([_row(0, mine_ok=False)] * 3 + [_row(0)] * 7) is not None
    assert models_eval.hopeless([_row(0, False, False)] * 3 + [_row(0)] * 7) is None


def test_the_deepseek_trial_would_have_stopped_after_ten_turns():
    """Its first ten on 2 October 2026: won 1, lost 5, same 4; four first
    steps wrong to Luna's none."""
    outcomes = [1, -1, -1, 0, -1, 0, -1, -1, 0, 0]
    steps = [False, True, True, False, False, False, True, True, True, True]
    rows = [_row(o, s) for o, s in zip(outcomes, steps, strict=True)]
    assert "first steps" in models_eval.hopeless(rows)


def test_an_evaluation_stops_after_the_batch_that_settles_it():
    """Thirty turns, judges that always prefer the incumbent: stopped after
    the first ten, with no switch."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from tests.unit.test_models_eval import _replying, _tools, _turn, _who

    def prefers_incumbent(messages, info):
        candidate_is_a = "<reply_a>\nfrom the candidate\n" in messages[-1].parts[-1].content
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "better": "B" if candidate_is_a else "A", "why": "the other is better"})])

    judge = (dataclasses.replace(BASE[Role.ESCALATE], slug="judge/x"),
             FunctionModel(prefers_incumbent))
    turns = [_turn(reply_id=i, tools=[]) for i in range(1, 31)]
    result = asyncio.run(models_eval.evaluate(
        turns, _who(*_replying("from the candidate")),
        _who(*_replying("from the incumbent"), name="incumbent", slug="openai/gpt-6-luna"),
        Role.CONVERSE, models_eval.Budget(1.0), tools=_tools(), judges=[judge, judge]))
    assert len(result.rows) == models_eval.BATCH
    assert result.measured is None and result.decision.switch is False
    assert result.decision.reason.startswith("stopped early: lost 10, won 0")


def test_triage_and_background_work_cannot_be_tried(conn):
    with pytest.raises(models_eval.TrialRefused, match="keeps no turns"):
        asyncio.run(models_eval.trial(conn, SLUG, Role.TRIAGE))


# --- the night's round -------------------------------------------------------------


def test_the_round_is_weekly(conn):
    assert scouting.due(conn) is True  # never scanned
    _candidate(conn, _facts())
    assert scouting.due(conn) is False
    assert scouting.due(conn, now=time.time() + 7 * 24 * 3600) is True


def test_a_round_not_yet_due_does_nothing_and_spends_nothing(monkeypatch, conn):
    async def must_not(*args, **kwargs):
        raise AssertionError("scanned when not due")

    monkeypatch.setattr(scouting.offloop, "with_own_conn", must_not)
    _candidate(conn, _facts())
    report = NightReport(started_at="t")
    assert asyncio.run(scouting.scout(conn, report)) == 0.0
    assert report.scouted == []


def test_the_morning_says_what_the_round_did(monkeypatch, conn):
    async def scanned(conn, fn, *args, **kwargs):
        return {"listed": 120, "new": ["openai/gpt-7-new"], "rows": []}

    async def lost(conn, slug, role, n=40, *, dry=False):
        return {"eval_id": 1, "spent_usd": 0.05,
                "decision": {"switch": False, "kind": "", "reason": "stopped early: lost 6"}}

    monkeypatch.setattr(scouting.offloop, "with_own_conn", scanned)
    monkeypatch.setattr(scouting.models_scout, "shortlist",
                        lambda conn: [{"slug": "openai/gpt-7-new", "role": Role.CONVERSE,
                                       "why": "released 2026-10-10, at 0.60x the cost"}])
    monkeypatch.setattr(scouting.models_eval, "trial", lost)
    report = NightReport(started_at="2026-10-09 03:00")
    assert asyncio.run(scouting.scout(conn, report)) == 0.05
    text = digest(report)
    assert "Models:" in text and "new: openai/gpt-7-new" in text
    assert "Tried openai/gpt-7-new for everyday replies" in text and "stopped early" in text
    # A no is a no: nothing about clearing the bar, and nothing switched.
    assert "cleared the bar" not in text and models_switch.read_switches() == {}


def test_a_round_that_fails_says_so_and_the_night_goes_on(monkeypatch, conn):
    async def broken(conn, fn, *args, **kwargs):
        raise RuntimeError("catalog down")

    monkeypatch.setattr(scouting.offloop, "with_own_conn", broken)
    report = NightReport(started_at="t")
    assert asyncio.run(scouting.scout(conn, report)) == 0.0
    assert report.scouted == ["Scouting stopped: RuntimeError: catalog down"]


def test_the_night_switches_only_through_the_guard(monkeypatch, conn):
    """Belt and braces with the gate test: `_maybe_switch` refuses when the
    guard does, with the guard's reason."""
    monkeypatch.setattr(guard, "load_policy", lambda *a, **k: ON)
    _candidate(conn, _facts())
    trial = _trial(conn)
    pick = {"slug": SLUG, "role": Role.CONVERSE, "why": "t"}
    out = {"eval_id": trial, "decision": dataclasses.asdict(YES)}
    line = scouting._maybe_switch(conn, pick, out)
    assert line.startswith("Switched everyday replies")
    assert models.MODELS[Role.CONVERSE].slug == SLUG
