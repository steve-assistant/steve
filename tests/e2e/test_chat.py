"""A message from the app is answered on the right tier, and the day shows it.

Each tier is reached through its own endpoint: the chat tier through Chat
Completions, the reasoning tier through Responses (`provider
.needs_responses_api`). A hard question reaches the second only when the
first model asks, by its `escalate` tool, and the turn is replayed there.
Tested apart, each half passed while the whole could not: a Responses
shape the SDK refused, or a replay that lost the question, would leave a
hard question unanswered with every unit test green.
"""

from __future__ import annotations

import pytest

from tests.e2e.backend import running
from tests.fakes.openai import FakeOpenAI, Rule
from tests.fakes.world import world

EASY = "What should I call a grey cat?"
NAMED = "Smoke, or Pebble."
HARD = "Which of the two campsites suits a family with a toddler?"
WEIGHED = "The lake site: shade, a shallow beach and the toilets close by."
RULES = (
    Rule(said=HARD, endpoint="chat", offered="escalate", call="escalate",
         arguments={"reason": "weighing two options against several needs"}),
    Rule(said=HARD, endpoint="responses", say=WEIGHED),
    Rule(said=EASY, endpoint="chat", say=NAMED),
)


@pytest.fixture(scope="module")
def steve(fenced):
    with (world(fenced / "world", openai=FakeOpenAI(RULES)) as made,
          running(made, fenced / "data") as backend):
        yield backend


async def test_a_message_is_answered_on_the_chat_tier_and_the_day_shows_both(steve):
    said = await steve.chat(EASY)

    assert said["type"] == "reply", steve.logs()
    assert said["text"] == NAMED and said["role"] == "converse"
    assert not said["escalated_from"]
    day = steve.get("/control/day").json()["entries"]
    assert [(e["kind"], e["text"]) for e in day if e["text"] in (EASY, NAMED)] == [
        ("said", EASY), ("reply", NAMED)]


async def test_a_hard_question_is_escalated_by_the_model_and_answered_through_responses(steve):
    before = len(steve.world.openai.requests)

    said = await steve.chat(HARD)

    assert said["type"] == "reply", steve.logs()
    assert said["text"] == WEIGHED
    assert (said["escalated_from"], said["role"]) == ("converse", "reason")
    assert said["escalation_reason"] == "weighing two options against several needs"
    paths = [kept.path for kept in steve.world.openai.requests[before:]]
    assert paths.index("/v1/chat/completions") < paths.index("/v1/responses")
    day = steve.get("/control/day").json()["entries"]
    assert [(e["kind"], e["text"]) for e in day if e["text"] in (HARD, WEIGHED)] == [
        ("said", HARD), ("reply", WEIGHED)]
