"""When Telegram misbehaves: rate limits, a long reply cut partway, and a
second program on the same bot.

The fake Bot API (tests/fakes/telegram.py) is told its troubles mid-run:
by method, the answers the next calls get instead of their own. Each case
checks that the reply reaches the chat whole and once, and that trouble
the user can fix is said on the app's Telegram page, which reads the
bridge's state alone.
"""

from __future__ import annotations

import json

import pytest

from channels.telegram import TAKEN
from tests.e2e.backend import Backend, briefed, running, until
from tests.e2e.test_hostile_signal import QUICKER
from tests.fakes.openai import FakeOpenAI, Rule
from tests.fakes.telegram import CONFLICT, USER, too_many
from tests.fakes.world import world

ASK_NAME = "What is a good name for a fern?"
NAMED = "Frond Henderson."
ASK_LONG = "Tell me everything about looking after a fern."
# Two paragraphs, each under Telegram's 4096 characters, together over it:
# two messages.
FIRST = "Ferns like it damp and shaded, as on a forest floor. " * 60
SECOND = "Water the soil, never the fronds, and mist them in winter. " * 55
LONG = FIRST.strip() + "\n\n" + SECOND.strip()
ASK_LIGHT = "Where should a fern stand?"
LIGHT = "In bright, indirect light, away from the radiator."
RULES = (Rule(said=ASK_NAME, say=NAMED), Rule(said=ASK_LONG, say=LONG),
         Rule(said=ASK_LIGHT, say=LIGHT))
REFUSED = (500, {"ok": False, "error_code": 500, "description": "Internal Server Error"})


@pytest.fixture(scope="module")
def steve(fenced):
    data = fenced / "data"
    (data / "telegram").mkdir(parents=True)
    (data / "telegram" / "state.json").write_text(
        json.dumps({"chat_id": USER, "offset": 0}), encoding="utf-8", newline="\n")
    with (world(fenced / "world", openai=FakeOpenAI(RULES), telegram=True,
                after_import=QUICKER)
          as made,
          running(made, data) as backend):
        assert until(lambda: bool(made.telegram.called("getUpdates")), within=60), backend.logs()
        briefed(backend)
        assert until(lambda: bool(made.telegram.delivered(USER))), backend.logs()
        yield backend


def arrived(steve: Backend, words: str) -> list[str]:
    """The messages the chat got that hold these words."""
    return [text for text in steve.world.telegram.delivered(USER) if words in text]


def said_on_the_page(steve: Backend) -> str | None:
    # A reply without `bridge` is the supervisor's, not the page's: CI's
    # runner once got one mid-test (22 October 2026), and a KeyError said
    # neither what came back nor what the backend logged.
    status = steve.get("/telegram/status")
    assert status.status_code == 200, (status.status_code, status.text, steve.logs())
    return status.json()["bridge"].get("error")


def test_a_reply_telegram_rate_limits_waits_the_time_it_names_and_arrives_once(steve):
    """Telegram refuses every call inside the window it names; the bridge's
    own tries, seconds apart, would each have been refused too."""
    fake = steve.world.telegram
    fake.trouble["sendMessage"] = [too_many(1)]
    fake.says(ASK_NAME)

    assert until(lambda: len(arrived(steve, NAMED)) == 1, within=30), steve.logs()
    assert not until(lambda: len(arrived(steve, NAMED)) > 1, within=3)
    assert len([p for p in fake.sent(USER) if NAMED in p.get("text", "")]) == 2


def test_a_long_reply_refused_partway_goes_on_from_where_it_stopped(steve):
    """The bridge sends a reply that failed again, whole; the first half
    came twice before."""
    fake = steve.world.telegram
    fake.trouble["sendMessage"] = [None, REFUSED]
    fake.says(ASK_LONG)

    assert until(lambda: len(arrived(steve, SECOND.strip()[:40])) == 1, within=30), steve.logs()
    assert len(arrived(steve, FIRST.strip()[:40])) == 1
    assert not until(lambda: len(arrived(steve, SECOND.strip()[:40])) > 1, within=3)


def test_another_program_on_the_bot_is_said_on_the_page_and_steve_answers_once_it_stops(steve):
    """Telegram gives each update to one poller. Before, the page showed
    Telegram ready while every message went to the other program."""
    fake = steve.world.telegram
    fake.trouble["getUpdates"] = [CONFLICT] * 100

    assert until(lambda: said_on_the_page(steve) == TAKEN, within=20), steve.logs()

    fake.trouble["getUpdates"].clear()
    fake.says(ASK_LIGHT)

    assert until(lambda: len(arrived(steve, LIGHT)) == 1, within=30), steve.logs()
    assert until(lambda: said_on_the_page(steve) is None, within=10)
