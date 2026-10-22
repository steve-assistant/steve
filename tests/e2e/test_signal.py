"""Signal, as a phone drives it: cards, yeses and a reminder by Note to Self.

The channel runs signal-cli's daemon and reads its event stream; the fake
daemon (tests/fakes/signal_cli.py) is the program `STEVE_SIGNAL_CLI`
names, so the worker starts it, links to it and talks JSON-RPC to it as
to the real one. The user writes to themselves; Steve answers there. What
is checked is what the phone shows: a card with its word, the reply to a
yes, a wrong word that runs nothing, a high-risk card that takes the
authenticator's code, a reminder at its time.
"""

from __future__ import annotations

import contextlib
import re
import time
from datetime import datetime, timedelta

import pytest

from agent import approvals, outbox
from core import db, totp
from core.consent import classify
from tests.e2e.backend import Backend, running, until
from tests.fakes import signal_cli
from tests.fakes.openai import FakeOpenAI, Rule
from tests.fakes.world import world

ME = ["maya.lindqvist@example.com"]
WEEK = {"title": "Week ahead", "instruction": "Write Maya the week ahead.", "at_time": "08:00",
        "days": ["mon"], "email_to": ME}
GARDEN = {**WEEK, "title": "Garden log", "days": ["sun"]}
ASK_WEEK = "Every Monday, plan my week and email it to me."
ASK_GARDEN = "Every Sunday, a garden log, emailed to me."
SET = "Set up. It starts once you say yes on the card."
BUY = {"amount_usd": 48.0, "merchant": "Zinc", "item": "a table for two"}
RULES = [
    Rule(said=ASK_WEEK, call="create_routine", arguments=WEEK),
    Rule(said=ASK_GARDEN, call="create_routine", arguments=GARDEN),
    Rule(result="", say=SET),
]


@pytest.fixture(scope="module")
def steve(fenced):
    with (world(fenced / "world", openai=FakeOpenAI(RULES), signal=True) as made,
          running(made, fenced / "data") as backend):
        # A card written before the channel listens waits a minute for its
        # retry (agent/outbox.py), so every test starts once it does.
        assert until(lambda: signal_cli.streams(made.signal) >= 1, within=60), backend.logs()
        yield backend


def told(steve: Backend, pattern: str, *, after: int) -> re.Match | None:
    """The first message Steve sent to the phone since `after` (a
    timestamp the daemon gave) that matches `pattern`."""
    for call in signal_cli.sent(steve.world.signal):
        if call["method"] == "send" and call["timestamp"] > after:
            found = re.search(pattern, call["params"].get("message", ""))
            if found:
                return found
    return None


def heard(steve: Backend, pattern: str, *, after: int, within: float = 30) -> re.Match:
    """`told`, waited for."""
    deadline = time.monotonic() + within
    while (found := told(steve, pattern, after=after)) is None and time.monotonic() < deadline:
        time.sleep(0.2)
    assert found, f"nothing sent to the phone matched {pattern!r}:\n{steve.logs()}"
    return found


def status(steve: Backend, title: str) -> str | None:
    found = [r["status"] for r in steve.get("/control/autonomy").json()["routines"]
             if r["title"] == title]
    return found[0] if found else None


def test_a_card_arrives_as_a_note_to_self_and_yes_with_its_word_runs_it(steve):
    asked = signal_cli.says(steve.world.signal, ASK_WEEK)
    word = heard(steve, r"Reply YES (\S+) to go ahead", after=asked)[1]
    # Waited for too: the card goes out as the tool makes it, the reply only
    # once the model has answered after it. Taken as already there, it was
    # missing on CI's slower runner (22 October 2026).
    heard(steve, re.escape(SET), after=asked)

    yes = signal_cli.says(steve.world.signal, f"YES {word}")

    heard(steve, rf"Going ahead with {word}.*UNDO {word}", after=yes)
    assert until(lambda: status(steve, "Week ahead") == "active"), steve.logs()


def test_a_yes_with_the_wrong_word_runs_nothing(steve):
    asked = signal_cli.says(steve.world.signal, ASK_GARDEN)
    word = heard(steve, r"Reply YES (\S+) to go ahead", after=asked)[1]
    wrong = "PARSNIP" if word != "PARSNIP" else "TURNIP"

    signal_cli.says(steve.world.signal, f"YES {wrong}")

    assert not until(lambda: status(steve, "Garden log") == "active", within=5)
    assert steve.card("Garden log")["word"] == word


def test_a_high_risk_card_on_the_phone_takes_a_yes_only_with_the_authenticators_code(steve):
    # No tool makes one in v0.1 (test_high_risk.py): made, and put in the
    # outbox for the phone, as the worker's `approvals._ask` does.
    with contextlib.closing(db.open_store(steve.data / "agent.db")) as conn:
        _, made = approvals.queue(conn, tool="purchase", args=dict(BUY),
                                  preview="Buy a table for two from Zinc: $48.00",
                                  consent=classify("purchase", BUY), channel="signal")
        assert made is not None
        start = int(time.time() * 1000)
        outbox.add(conn, approvals.card(made), kind="card", remote="signal", action_id=made.id)

        word = heard(steve, r"reply YES (\S+) and the 6-digit code", after=start)[1]
        bare = signal_cli.says(steve.world.signal, f"YES {word}")
        heard(steve, rf"Not done: this one needs the 6-digit code.*\n  {word}  Buy a table",
              after=bare)
        assert approvals.get(conn, made.id).status == "pending"

        code = totp.code_at(steve.world.secrets["APPROVAL_TOTP_SECRET"], time.time())
        signal_cli.says(steve.world.signal, f"YES {word} {code}")

        assert until(lambda: approvals.get(conn, made.id).status != "pending"), steve.logs()


def test_a_reminder_set_by_message_reaches_the_phone_and_the_day_and_waits_to_be_done(steve):
    at = (datetime.now() + timedelta(seconds=3)).isoformat(timespec="seconds")
    steve.world.openai.rules.insert(0, Rule(said="water the plants", call="remind_me",
                                            arguments={"what": "Water the plants", "when": at}))
    asked = signal_cli.says(steve.world.signal, "Remind me to water the plants in a moment.")

    heard(steve, r"Water the plants", after=asked)

    day = steve.get("/control/day").json()["entries"]
    assert [e for e in day if e["kind"] == "update" and "Water the plants" in e["text"]]
    note = [n for n in steve.get("/control/needs").json()["needs"]
            if n["kind"] == "now" and "Water the plants" in n["text"]]
    assert len(note) == 1 and note[0]["actions"] == ["done", "later"]
    assert steve.post("/control/needs/done", {"id": note[0]["id"]}).json()["ok"]
    assert not [n for n in steve.get("/control/needs").json()["needs"]
                if "Water the plants" in n["text"]]
