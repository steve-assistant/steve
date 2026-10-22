"""A card from the app: a yes runs it after the hold, a no during the hold
undoes it, and a no runs nothing.

Each routine here may email without asking, so creating one makes a card
(`approve_routine`, irreversible). The card is read where the app reads
it, from the notes (`/control/needs`), and answered where the app answers
it (`/actions/decide`), with the card's hash as read: the whole path a
user's yes takes, through the worker's own queue and the hold, which the
unit tests drive a step at a time. A turn's cards join into one only for
one account (`approvals._join`), so a yes to part of a card is the mail
scenarios' (test_mail.py).
"""

from __future__ import annotations

import pytest

from tests.e2e.backend import Backend, running, until
from tests.fakes.openai import FakeOpenAI, Rule
from tests.fakes.world import world

ME = ["maya.lindqvist@example.com"]


def routine(title: str, days: list[str]) -> dict:
    return {"title": title, "instruction": f"Write Maya the {title.lower()}.",
            "at_time": "08:00", "days": days, "email_to": ME}


WEEK, FRIDAY = routine("Week ahead", ["mon"]), routine("Friday review", ["fri"])
GARDEN = routine("Garden log", ["sun"])
ASK_WEEK = "Every Monday, plan my week and email it to me."
ASK_FRIDAY = "Every Friday, review my week and email it to me."
ASK_GARDEN = "Every Sunday, write up the garden and email it to me."
RULES = (
    Rule(said=ASK_WEEK, call="create_routine", arguments=WEEK),
    Rule(said=ASK_FRIDAY, call="create_routine", arguments=FRIDAY),
    Rule(said=ASK_GARDEN, call="create_routine", arguments=GARDEN),
    Rule(result="", say="Set up. It starts once you say yes on the card."),
)


@pytest.fixture(scope="module")
def steve(fenced):
    with (world(fenced / "world", openai=FakeOpenAI(RULES)) as made,
          running(made, fenced / "data") as backend):
        yield backend


def status(steve: Backend, title: str) -> str | None:
    found = [r["status"] for r in steve.get("/control/autonomy").json()["routines"]
             if r["title"] == title]
    return found[0] if found else None


async def test_a_yes_in_the_app_runs_the_card_once_the_hold_is_over(steve):
    said = await steve.chat(ASK_WEEK)
    assert said["type"] == "reply", steve.logs()
    assert status(steve, "Week ahead") == "awaiting_approval"

    assert steve.decide(steve.card("Week ahead"), True).json()["ok"]

    assert until(lambda: status(steve, "Week ahead") == "active"), steve.logs()


def action(steve: Backend, note: dict) -> str:
    """The card's action's status, as the app's activity page has it."""
    return next(a["status"] for a in steve.get("/control/activity").json()["items"]
                if a["kind"] == "action" and a["id"] == note["action_id"])


async def test_a_no_during_the_hold_undoes_the_yes(steve):
    """The hold exists for this: a yes given by mistake can be taken back
    before anything irreversible happens. A no to a card already said yes
    to is that undo, through the same door (agent/worker/answering.py)."""
    said = await steve.chat(ASK_GARDEN)
    assert said["type"] == "reply", steve.logs()
    note = steve.card("Garden log")
    assert steve.decide(note, True).json()["ok"]
    assert until(lambda: action(steve, note) == "executing"), steve.logs()

    undone = steve.decide(note, False).json()

    assert undone["ok"] and undone["undone"], undone
    assert not until(lambda: status(steve, "Garden log") == "active", within=4)
    assert action(steve, note) == "undone"
    assert status(steve, "Garden log") is None  # ended, so no longer listed


async def test_a_no_in_the_app_runs_nothing(steve):
    said = await steve.chat(ASK_FRIDAY)
    assert said["type"] == "reply", steve.logs()

    assert steve.decide(steve.card("Friday review"), False).json()["ok"]

    # Ended, so no longer listed: not left waiting for a yes with no card
    # to give it on.
    assert status(steve, "Friday review") is None
    assert not [n for n in steve.get("/control/needs").json()["needs"]
                if "Friday review" in n["text"]]
