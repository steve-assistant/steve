"""The fake Jev answers `core/decide.py` over HTTP as the real one does.

A worker the end-to-end tests start asks its decisions -- the screen, the
critic, splitting tasks -- over a socket, at the address
`STEVE_OPENROUTER_URL` names (core/endpoints.py). In a test's own process
the fences let `decide._post` through to such a fake on this computer and
refuse the real service (tests/gate/fences.py). So `decide.ask` itself is
driven here, request and reply, as the worker's would be: a fake whose
paths or shapes drifted would leave every screening unanswered, and every
scenario refused far from the cause.
"""

from __future__ import annotations

import os

import httpx2

from agent.attention.sources import health
from core import decide
from core.decide import Choice, Predicate
from tests.fakes.jev import BROKE, GOOD, FakeJev

QUESTIONS = [Predicate("urgent", "Is it urgent?"),
             Choice("kind", "What is it?", {"bill": "a bill", "note": "a note"})]


def test_a_decision_is_asked_at_the_fake_and_answered_by_question_name(monkeypatch):
    fake = FakeJev(urgent=0.9, kind="bill")
    with fake.running() as url:
        monkeypatch.setenv("STEVE_OPENROUTER_URL", url)
        decision = decide.ask(None, "Your invoice is due Friday", QUESTIONS, key=GOOD)
    assert decision.error == ""
    assert decision.answers["urgent"].probability == 0.9
    assert decision.answers["kind"].choice == "bill"
    assert fake.asked == [("/api/alpha/decisions", GOOD)]
    # Kept whole, for the canaries' scan (tests/fakes/world.py).
    assert "Your invoice is due Friday" in fake.requests[0].body


def test_a_predicate_no_one_scripted_takes_the_worlds_default(monkeypatch):
    """Unanswered, a question falls to the local readers, which a test's
    worker has not got: a world without a default refused every screening."""
    with FakeJev(otherwise=0.1).running() as url:
        monkeypatch.setenv("STEVE_OPENROUTER_URL", url)
        decision = decide.ask(None, "Lunch?", QUESTIONS[:1], key=GOOD)
    assert decision.answers["urgent"].probability == 0.1


def test_a_refused_key_gets_the_services_refusal_and_no_answers(monkeypatch):
    with FakeJev(urgent=0.9).running() as url:
        monkeypatch.setenv("STEVE_OPENROUTER_URL", url)
        decision = decide.ask(None, "Lunch?", QUESTIONS[:1], key="sk-or-v1-not-a-key")
        broke = decide.ask(None, "Lunch?", QUESTIONS[:1], key=BROKE)
    assert decision.answers == {} and decision.error.startswith("HTTP 401")
    assert broke.answers == {} and broke.error.startswith("HTTP 402")


def test_the_credit_is_served_where_the_health_check_and_setup_read_it(monkeypatch):
    """OpenRouter's credit moved here from the fake OpenAI: it is
    OpenRouter's, and a world serving both would otherwise answer it twice."""
    with FakeJev().running() as url:
        monkeypatch.setenv("STEVE_OPENROUTER_URL", url)
        assert health.openrouter_credits() == f"{url}/v1/credits"
        headers = {"Authorization": f"Bearer {GOOD}"}
        good = httpx2.get(health.openrouter_credits(), headers=headers).json()
        refused = httpx2.get(health.openrouter_credits())
    assert good == {"data": {"total_credits": 10.0, "total_usage": 2.5}}
    assert refused.status_code == 401


def test_without_a_fake_named_jev_is_still_refused():
    """The fence's door opens only to an address on this computer."""
    assert "STEVE_OPENROUTER_URL" not in os.environ
    decision = decide.ask(None, "Lunch?", QUESTIONS[:1], key=GOOD)
    assert decision.answers == {} and "tried to reach live Jev" in decision.error
