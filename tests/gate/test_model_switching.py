"""Who may switch the agent's models, and what puts a switch back.

The maintainer, 1 October 2026: switch on its own to a better or cheaper model, watch
it for a day, put it back if things get worse, and let them undo it with two
words. The switching is mutable code (agent/models_switch.py,
agent/night/scouting.py); what lets it happen is the frozen policy and
core/models_guard.py, and these tests -- frozen too -- pin that the mutable
code asks:

- the night switches only while `models.auto_switch` is on, and only to a
  model the bar said yes to; the user by hand may switch to anything the bounds
  admit;
- the day's watch puts a role back when the agent does worse with the new
  model -- more failures, slower calls, dearer calls;
- "REVERT MODELS" is answered before any model reads it, and puts every
  role back.

What it pins is the mechanism, not the setting: turning `auto_switch` on is
a one-line edit to policy.yaml and needs nothing changed here.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time

import pytest

from agent import models, models_switch
from agent.models import BASE, Role
from core import canary, db
from core import models_guard as guard
from core.models_guard import BY_HAND, Calls, Candidate, Decision, Priced
from core.policy import load_policy

POLICY = load_policy()
OFF = dataclasses.replace(POLICY, models=dataclasses.replace(POLICY.models, auto_switch=False))
ON = dataclasses.replace(POLICY, models=dataclasses.replace(POLICY.models, auto_switch=True))
YES = Decision(True, "cheaper", "38% cheaper; won 5, lost 4 of 40")
NO = Decision(False, "", "lost 20 of 40")
# A model OpenAI might release next, at half Luna's price, put into the
# guard's frozen table for each test that switches to it.
SLUG = "openai/gpt-7-luna"
NEXT = Priced(0.05, 0.005, 0.25, context=1_050_000, max_output=128_000,
              efforts=("none", "low", "medium", "high"))


# --- who may switch --------------------------------------------------------------


@pytest.mark.parametrize("decision", [YES, NO, None])
def test_the_user_by_hand_may_switch_whatever_the_bar_said(decision):
    """Their call; the bounds still hold when the worker loads it."""
    assert guard.switch_refusal(BY_HAND, decision, OFF) is None


@pytest.mark.parametrize("by", ["night", "watch", "agent", ""])
def test_nothing_else_switches_while_switching_on_its_own_is_off(by):
    assert "off" in guard.switch_refusal(by, YES, OFF)


@pytest.mark.parametrize("decision", [NO, None])
def test_the_night_switches_only_to_a_model_the_bar_said_yes_to(decision):
    assert "did not say yes" in guard.switch_refusal("night", decision, ON)
    assert guard.switch_refusal("night", YES, ON) is None


# --- the watch -------------------------------------------------------------------


QUIET = canary.Counts(turns=40, bad=2, by_kind={"tool_error": 2})
OLD = Calls(40, 0.9, 0.0010)


def test_more_failures_after_a_switch_put_it_back():
    worse = canary.Counts(turns=20, bad=8, by_kind={"turn_broke": 8})
    assert guard.watch_verdict(QUIET, worse, OLD, OLD, POLICY) is not None


@pytest.mark.parametrize("new,said", [
    (Calls(20, 1.5, 0.0010), "takes"),       # 1.67x the time a call
    (Calls(20, 0.9, 0.0025), "costs"),       # 2.5x the cost a call
    (Calls(20, float("nan"), 0.0010), "takes"),
    (Calls(20, 0.9, float("nan")), "costs"),
])
def test_slower_or_dearer_calls_after_a_switch_put_it_back(new, said):
    """NaN would pass any comparison written as "worse if above": it counts
    as worse."""
    assert said in guard.watch_verdict(QUIET, QUIET, OLD, new, POLICY)


def test_a_switch_doing_as_well_is_left_alone():
    assert guard.watch_verdict(QUIET, QUIET, OLD, Calls(20, 0.95, 0.0006), POLICY) is None
    # Too few calls on either side are not a measure.
    assert guard.watch_verdict(QUIET, QUIET, OLD, Calls(3, 9.0, 0.5), POLICY) is None


# --- the switching code asks ------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A database and a switch file of the test's own; the lineup as
    written afterwards."""
    monkeypatch.setattr(models, "active_path", lambda: tmp_path / "model_switches.json")
    monkeypatch.setitem(guard.PRICED, SLUG, NEXT)
    conn = db.open_store(tmp_path / "gate.db")
    facts = Candidate(SLUG)
    conn.execute("INSERT INTO model_candidates (slug, facts, roles, eligible) VALUES (?, ?, ?, 5)",
                 (SLUG, json.dumps(facts.to_dict()), json.dumps({r: None for r in
                                                                  POLICY.models.auto_roles})))
    conn.commit()
    yield conn
    conn.close()
    models.load_active(tmp_path / "__none__.json")


def _trial(conn, decision: Decision, role: str = "converse") -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO model_evals (slug, role, incumbent, decision, finished_at)"
            " VALUES (?, ?, ?, ?, datetime('now'))",
            (SLUG, role, BASE[Role(role)].slug, json.dumps(dataclasses.asdict(decision))))
    return int(cur.lastrowid)


def _policy(monkeypatch, policy):
    monkeypatch.setattr(guard, "load_policy", lambda *a, **k: policy)


def test_the_night_cannot_switch_while_it_is_off_however_good_the_trial(store, monkeypatch):
    _policy(monkeypatch, OFF)
    _trial(store, YES)
    with pytest.raises(models_switch.SwitchRefused, match="off"):
        models_switch.switch(store, SLUG, Role.CONVERSE, by="night")
    assert models_switch.read_switches() == {}


def test_the_night_switches_when_it_is_on_and_the_bar_said_yes(store, monkeypatch):
    _policy(monkeypatch, ON)
    with pytest.raises(models_switch.SwitchRefused, match="did not say yes"):
        _trial(store, NO)
        models_switch.switch(store, SLUG, Role.CONVERSE, by="night")
    _trial(store, YES)
    models_switch.switch(store, SLUG, Role.CONVERSE, by="night")
    assert set(models_switch.read_switches()) == {"converse"}


def test_the_nights_round_asks_the_guard_before_it_switches(store, monkeypatch):
    """The weekly round (agent/night/scouting.py), with a trial that cleared
    the bar: nothing switched while switching on its own is off."""
    from agent.night import scouting
    from agent.night.report import NightReport

    async def cleared(conn, slug, role, n=40, *, dry=False):
        return {"eval_id": _trial(conn, YES), "spent_usd": 0.05,
                "decision": dataclasses.asdict(YES)}

    async def scanned(conn, fn, *args, **kwargs):
        return {"listed": 1, "new": [], "rows": []}

    monkeypatch.setattr(scouting.offloop, "with_own_conn", scanned)
    monkeypatch.setattr(scouting.models_scout, "shortlist",
                        lambda conn: [{"slug": SLUG, "role": Role.CONVERSE, "why": "test"}])
    monkeypatch.setattr(scouting.models_eval, "trial", cleared)

    def a_week_on():
        store.execute("UPDATE model_candidates SET last_seen = '2000-01-01 00:00:00'")
        store.commit()

    _policy(monkeypatch, OFF)
    a_week_on()
    report = NightReport(started_at="test")
    asyncio.run(scouting.scout(store, report, now=time.time()))
    assert models_switch.read_switches() == {}
    assert any("not switched" in line for line in report.scouted)

    _policy(monkeypatch, ON)
    a_week_on()
    report = NightReport(started_at="test")
    asyncio.run(scouting.scout(store, report, now=time.time()))
    assert set(models_switch.read_switches()) == {"converse"}
    assert models.MODELS[Role.CONVERSE].slug == SLUG


def test_one_switch_is_watched_at_a_time(store, monkeypatch):
    _policy(monkeypatch, ON)
    _trial(store, YES, "converse")
    _trial(store, YES, "reason")
    models_switch.switch(store, SLUG, Role.CONVERSE, by="night")
    with pytest.raises(models_switch.SwitchRefused, match="still being watched"):
        models_switch.switch(store, SLUG, Role.REASON, by="night")


def _bad_events(conn, n: int, at: str) -> None:
    for i in range(n):
        cur = conn.execute("INSERT INTO issues (kind, fingerprint, summary) VALUES"
                           " ('turn_broke', ?, 'A turn broke: ValueError')", (f"t{at}{i}",))
        conn.execute("INSERT INTO issue_events (issue_id, at) VALUES (?, ?)",
                     (cur.lastrowid, at))
    conn.commit()


def _replies(conn, n: int, at: str) -> None:
    session = conn.execute("INSERT INTO sessions (channel) VALUES ('signal')").lastrowid
    conn.executemany("INSERT INTO messages (session_id, role, content, created_at)"
                     " VALUES (?, 'assistant', 'ok', ?)", [(session, at)] * n)
    conn.commit()


def test_a_watch_that_sees_the_agent_doing_worse_puts_the_role_back(store, monkeypatch):
    _policy(monkeypatch, ON)
    _trial(store, YES)
    started = time.time() - 3 * 3600
    _replies(store, 30, canary.sqltime(started - 24 * 3600))
    models_switch.switch(store, SLUG, Role.CONVERSE, by="night", now=started)
    _replies(store, 10, canary.sqltime(started + 600))
    _bad_events(store, 6, canary.sqltime(started + 900))

    said = models_switch.tick(store)
    assert said and "Put everyday replies back" in said[0]
    assert models_switch.read_switches() == {}
    row = store.execute("SELECT outcome FROM model_switches WHERE kind = 'switch'").fetchone()
    assert row["outcome"].startswith("reverted")


def test_revert_models_is_answered_before_any_model_and_puts_every_role_back(store, monkeypatch):
    from agent import runtime
    from agent.worker import answering

    _policy(monkeypatch, ON)
    _trial(store, YES)
    models_switch.switch(store, SLUG, Role.CONVERSE, by="night")
    models_switch.apply()
    assert models.MODELS[Role.CONVERSE].slug == SLUG
    monkeypatch.setattr(runtime, "conn", store)

    # Not from a channel that is not their own words.
    assert asyncio.run(answering.intercept_approval("REVERT MODELS", "initiative")) is None
    said = asyncio.run(answering.intercept_approval("revert models", "signal"))
    assert "Put back" in said
    assert models_switch.read_switches() == {}
    assert models.MODELS[Role.CONVERSE] == BASE[Role.CONVERSE]
