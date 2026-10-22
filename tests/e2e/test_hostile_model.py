"""When OpenAI, or what stands in front of it, misbehaves.

Each case checks the same three things: the user is told plainly or gets
the answer after all, nothing the turn did is done twice or lost, and the
worker stays up to answer the next message. The fake fails as the real
service was seen to (tests/fakes/openai.py `FAILS`), each case on its own
words, so the cases share one backend: a 500 three times running, which
outlasts the SDK's own two retries; a rate limit that says when to try
again; a reply cut off mid-stream; and a captive portal's page in place of
an answer.

The app's chat streams its turn, as the phones' do not, so a cut there
reaches the turn as a dropped connection rather than an HTTP error.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.e2e.backend import Backend, running
from tests.fakes.openai import FakeOpenAI, Rule
from tests.fakes.world import world

TOMORROW_NINE = (datetime.now() + timedelta(days=1)).replace(
    hour=9, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
ASK_REMIND = "Remind me to repot the fern tomorrow at nine."
REMINDED = "I'll remind you to repot the fern tomorrow at nine."
ASK_BUSY = "What is a good name for a fern?"
NAMED = "Frond Henderson."
ASK_CUT = "How often should I water a fern?"
WATERING = "Once a week, when the top of the soil is dry, and mist it in between."
ASK_PORTAL = "Is a fern safe for cats?"
ASK_AFTER = "Thanks, that is all for now."
AFTER = "Glad to help."
RULES = (
    Rule(said=ASK_REMIND, call="remind_me",
         arguments={"what": "Repot the fern", "when": TOMORROW_NINE}),
    Rule(after="remind_me", fail="500", times=3),
    Rule(after="remind_me", say=REMINDED),
    Rule(said=ASK_BUSY, fail="429", times=1),
    Rule(said=ASK_BUSY, say=NAMED),
    Rule(said=ASK_CUT, fail="cut", say=WATERING, times=1),
    Rule(said=ASK_CUT, say=WATERING),
    Rule(said=ASK_PORTAL, fail="garbage"),
    Rule(said=ASK_AFTER, say=AFTER),
)


@pytest.fixture(scope="module")
def steve(fenced):
    # The turn's own retries wait as they would, but a twentieth as long
    # (agent/llm.py `backoff_delay`): six tries of a portal's page would
    # otherwise take half a minute.
    with (world(fenced / "world", openai=FakeOpenAI(RULES),
                after_import={"agent.llm": "BASE_BACKOFF_S = 0.05"}) as made,
          running(made, fenced / "data") as backend):
        yield backend


def asked(steve: Backend, words: str) -> int:
    """How many requests reached the fake with these words last."""
    return sum(1 for kept in steve.world.openai.requests
               if isinstance(kept.body, dict) and words in str(kept.body))


def said_today(steve: Backend, words: str) -> list[str]:
    return [e["kind"] for e in steve.get("/control/day").json()["entries"]
            if words in (e.get("text") or "")]


async def test_a_server_error_after_a_tool_is_tried_again_and_the_tool_runs_once(steve):
    """Three 500s outlast the SDK's retries, so the turn itself starts
    again from the top, and the model asks for the reminder a second time.
    The turn has already set it: the user gets one reminder, not two."""
    said = await steve.chat(ASK_REMIND)

    assert said["type"] == "reply", steve.logs()
    assert said["text"] == REMINDED
    fern = [r for r in steve.get("/reminders").json()["active"]
            if r["text"] == "Repot the fern"]
    assert len(fern) == 1, fern


async def test_a_rate_limit_is_waited_out_and_answered_once(steve):
    said = await steve.chat(ASK_BUSY)

    assert said["type"] == "reply", steve.logs()
    assert said["text"] == NAMED
    assert said_today(steve, NAMED) == ["reply"]


async def test_a_reply_cut_off_mid_stream_is_tried_again_and_arrives_whole(steve):
    """A connection that drops partway through is the commonest failure
    on a laptop's network. The app's chat streams, so the turn sees the
    drop itself, after the SDK has handed over the stream and cannot try
    again."""
    said = await steve.chat(ASK_CUT)

    assert said["type"] == "reply", said
    assert said["text"] == WATERING
    assert said_today(steve, WATERING) == ["reply"]


async def test_a_page_that_is_not_openais_answer_is_said_plainly_and_loses_nothing(steve):
    """A hotel's network answers every request with its sign-in page. The
    turn tries again, as for any unreadable answer, and then says so in
    words, with the user's message kept and the worker answering the next."""
    said = await steve.chat(ASK_PORTAL)

    assert said["type"] == "error", said
    assert "Something broke on my side before I could finish that" in said["error"]
    assert "try again" in said["error"]
    assert said_today(steve, ASK_PORTAL) == ["said"]
    assert asked(steve, ASK_PORTAL) > 1

    after = await steve.chat(ASK_AFTER)
    assert (after["type"], after.get("text")) == ("reply", AFTER), steve.logs()
