"""At the month's cap, "RAISE 15" raises it, from the phones and the app.

The cap stops every model, so a request to raise it could only be answered
"I've reached the limit"; the Spend page was the one way out, and it is not
on a phone. "RAISE 15", the whole message, is read in code: it makes the
raise's card with no model call, on Telegram, on Signal and in the app's
chat alike. The card is high-risk, as a purchase is, so one waits at a time
and its yes in the app takes the passphrase. Once said yes to, the cap is
$15 and the next message is answered by the model again.

The test starts its own backend: a cap reached stays so for the month, and
would answer any test after it.
"""

from __future__ import annotations

import contextlib
import json
import secrets
from collections.abc import Iterator

import pytest

from core import db, spend
from tests.e2e.backend import Backend, running, until
from tests.fakes import signal_cli
from tests.fakes.openai import FakeOpenAI, Rule
from tests.fakes.telegram import USER
from tests.fakes.world import world

ASKED = "Anything on today?"
ANSWER = "Nothing on today."
RULES = (Rule(said=ASKED, say=ANSWER),)


@pytest.fixture(scope="module")
def steve(fenced) -> Iterator[Backend]:
    data = fenced / "data"
    (data / "telegram").mkdir(parents=True)
    (data / "telegram" / "state.json").write_text(
        json.dumps({"chat_id": USER, "offset": 0}), encoding="utf-8", newline="\n")
    with (world(fenced / "world", openai=FakeOpenAI(RULES), telegram=True, signal=True) as made,
          running(made, data) as backend):
        assert until(lambda: bool(made.telegram.called("getUpdates")), within=60), backend.logs()
        assert until(lambda: signal_cli.streams(made.signal) >= 1, within=60), backend.logs()
        yield backend


def asked_of_the_model(steve: Backend, words: str) -> int:
    return sum(words in json.dumps(kept.body) for kept in steve.world.openai.requests)


def month_cap(steve: Backend) -> float:
    return json.loads((steve.data / "knobs.json").read_text(encoding="utf-8"))["MONTH_USD"]


def on_telegram(steve: Backend, words: str) -> bool:
    return any(words in params.get("text", "") for params in steve.world.telegram.sent(USER))


def on_signal(steve: Backend, words: str) -> bool:
    return any(call["method"] == "send" and words in call["params"].get("message", "")
               for call in signal_cli.sent(steve.world.signal))


async def test_raise_at_the_months_cap_asks_from_each_phone_and_the_apps_yes_raises_it(steve):
    typed = f"harbour lantern {secrets.token_hex(4)}"
    steve.world.keep("PASSPHRASE", typed)
    assert steve.post("/approvals/passphrase", {"new": typed}).json()["ok"]
    session = steve.post("/signin/passphrase", {"passphrase": typed}).json()["session"]
    steve.world.keep("SESSION", session)
    assert steve.post("/control/knobs", {"changes": {"MONTH_USD": 5}}).json()["ok"]
    with (contextlib.closing(db.open_store(steve.data / "agent.db")) as conn,
          db.transaction(conn)):
        spend.bill(conn, 5.0, model="gpt-5-mini", unit=spend.Unit("background"))

    steve.world.telegram.says("RAISE 12")
    assert until(lambda: on_telegram(
        steve, "I've asked for your yes: raise the month's spending cap from $5.00 to $12.00."),
        within=30), steve.logs()
    by_telegram = steve.card("from $5.00 to $12.00", kind="code")
    assert steve.decide(by_telegram, False, session=session).json()["ok"]

    signal_cli.says(steve.world.signal, "RAISE 14")
    assert until(lambda: on_signal(
        steve, "I've asked for your yes: raise the month's spending cap from $5.00 to $14.00."),
        within=30), steve.logs()
    by_signal = steve.card("from $5.00 to $14.00", kind="code")
    assert steve.decide(by_signal, False, session=session).json()["ok"]

    said = await steve.chat("RAISE 15")
    assert said["text"] == ("I've asked for your yes: raise the month's spending cap from"
                            " $5.00 to $15.00."), steve.logs()
    in_the_app = steve.card("from $5.00 to $15.00", kind="code")
    assert month_cap(steve) == 5
    taken = steve.decide(in_the_app, True, session=session, passphrase=typed)
    assert taken.status_code == 200 and taken.json()["ok"], taken.text
    assert until(lambda: month_cap(steve) == 15, within=30), steve.logs()

    said = await steve.chat(ASKED)

    assert said["text"] == ANSWER, steve.logs()
    assert asked_of_the_model(steve, "RAISE") == 0
