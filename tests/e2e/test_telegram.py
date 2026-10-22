"""Telegram, as a phone drives it: a card's buttons, and a stranger ignored.

The bot's token is the world's canary, kept in its keychain; the chat is
paired as a finished pairing leaves it (`telegram/state.json`). The fake
Bot API (tests/fakes/telegram.py) serves the long poll the channel makes
and keeps every call. A card comes with Yes and No buttons; a press is a
yes or a no only from the paired chat; anyone else who finds the bot is
answered by nothing, not even the model.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

import pytest

from tests.e2e.backend import Backend, running, until
from tests.fakes.openai import FakeOpenAI, Rule
from tests.fakes.telegram import STRANGER, USER
from tests.fakes.world import world

ME = ["maya.lindqvist@example.com"]
WEEK = {"title": "Week ahead", "instruction": "Write Maya the week ahead.", "at_time": "08:00",
        "days": ["mon"], "email_to": ME}
GARDEN = {**WEEK, "title": "Garden log", "days": ["sun"]}
ASK_WEEK = "Every Monday, plan my week and email it to me."
ASK_GARDEN = "Every Sunday, a garden log, emailed to me."
RULES = (
    Rule(said=ASK_WEEK, call="create_routine", arguments=WEEK),
    Rule(said=ASK_GARDEN, call="create_routine", arguments=GARDEN),
    Rule(result="", say="Set up. It starts once you say yes on the card."),
)


@pytest.fixture(scope="module")
def steve(fenced):
    data = fenced / "data"
    (data / "telegram").mkdir(parents=True)
    (data / "telegram" / "state.json").write_text(
        json.dumps({"chat_id": USER, "offset": 0}), encoding="utf-8", newline="\n")
    with (world(fenced / "world", openai=FakeOpenAI(RULES), telegram=True) as made,
          running(made, data) as backend):
        assert until(lambda: bool(made.telegram.called("getUpdates")), within=60), backend.logs()
        yield backend


def buttons(steve: Backend, words: str) -> dict[str, str]:
    """The buttons on the message sent to the paired chat with these
    words: each one's label and what pressing it sends."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        for params in steve.world.telegram.sent(USER):
            markup = params.get("reply_markup")
            if words in params.get("text", "") and markup:
                markup = json.loads(markup) if isinstance(markup, str) else markup
                return {b["text"]: b["callback_data"]
                        for row in markup["inline_keyboard"] for b in row}
        time.sleep(0.2)
    raise AssertionError(f"no card with {words!r} came:\n{steve.logs()}")


def status(steve: Backend, title: str) -> str | None:
    found = [r["status"] for r in steve.get("/control/autonomy").json()["routines"]
             if r["title"] == title]
    return found[0] if found else None


def test_a_cards_yes_button_runs_it(steve):
    steve.world.telegram.says(ASK_WEEK)
    pressed = buttons(steve, "Week ahead")
    assert re.fullmatch(r"YES [A-Z]+ \d+", pressed["Yes"])
    assert re.fullmatch(r"NO [A-Z]+ \d+", pressed["No"])

    steve.world.telegram.presses(pressed["Yes"])

    assert until(lambda: status(steve, "Week ahead") == "active"), steve.logs()


def test_a_cards_no_button_runs_nothing(steve):
    steve.world.telegram.says(ASK_GARDEN)
    pressed = buttons(steve, "Garden log")

    steve.world.telegram.presses(pressed["No"])

    # Ended, so no longer listed: not left waiting for a yes.
    assert until(lambda: status(steve, "Garden log") is None), steve.logs()


async def test_a_stranger_who_finds_the_bot_is_answered_by_nothing(steve):
    asked = len(steve.world.openai.requests)

    steve.world.telegram.says(ASK_WEEK, chat=STRANGER)
    steve.world.telegram.presses("YES ANYTHING", chat=STRANGER)

    await asyncio.wait_for(steve.world.telegram.caught_up(), 30)
    assert steve.world.telegram.sent(STRANGER) == []
    assert len(steve.world.openai.requests) == asked
