"""When Signal misbehaves: sends refused, and the daemon gone.

signal-cli is a Java program the worker starts and talks to over HTTP
(channels/signal.py); either half can fail under the other. A send fails
when the network does, and the daemon dies when its JVM is killed or runs
out of memory. Each case checks that the reply still reaches the phone,
once, and that a daemon that died is started again, so Steve does not go
quiet on the phone for good while the app shows it ready.

The fake daemon (tests/fakes/signal_cli.py) is told its troubles through
the folder it shares with the test: `fails` and `dies`.
"""

from __future__ import annotations

import pytest

from tests.e2e.backend import Backend, briefed, running, until
from tests.e2e.test_signal import heard, told
from tests.fakes import signal_cli
from tests.fakes.openai import FakeOpenAI, Rule
from tests.fakes.world import world

ASK_NAME = "What is a good name for a fern?"
NAMED = "Frond Henderson."
ASK_WATER = "How often should I water a fern?"
WATERING = "Once a week, when the top of the soil is dry."
ASK_LIGHT = "Where should a fern stand?"
LIGHT = "In bright, indirect light, away from the radiator."
RULES = (Rule(said=ASK_NAME, say=NAMED), Rule(said=ASK_WATER, say=WATERING),
         Rule(said=ASK_LIGHT, say=LIGHT))
# The bridge's tries at a reply that did not go, a tenth as far apart
# (channels/bridge.py), and the outbox's after a second rather than a
# minute (agent/outbox.py): the morning's briefing, met by Signal still
# starting, would otherwise be sent in the middle of a case.
QUICKER = {"channels.bridge": "REPLY_RETRIES_S = (0.2, 0.5, 1.5, 3.0)",
           "agent.outbox": "RETRY_AFTER = (timedelta(seconds=1),) * 5"}


@pytest.fixture(scope="module")
def steve(fenced):
    with (world(fenced / "world", openai=FakeOpenAI(RULES), signal=True,
                after_import=QUICKER)
          as made,
          running(made, fenced / "data") as backend):
        assert until(lambda: signal_cli.streams(made.signal) >= 1, within=60), backend.logs()
        briefed(backend)
        assert until(lambda: any(c["method"] == "send" for c in signal_cli.sent(made.signal))), \
            backend.logs()
        yield backend


def sends(steve: Backend, words: str) -> list[dict]:
    """Every send the daemon took with these words."""
    return [call for call in signal_cli.sent(steve.world.signal)
            if call["method"] == "send" and words in call["params"].get("message", "")]


def said_on_the_page(steve: Backend) -> str | None:
    """The trouble the app's Signal page shows: it reads the bridge's state."""
    return steve.get("/signal/status").json()["bridge"].get("error")


def test_a_reply_signal_refuses_three_times_is_tried_until_it_goes_and_arrives_once(steve):
    """The answer is the one thing the user is waiting for. Two tries two
    seconds apart lost it to a network that came back a few seconds later."""
    signal_cli.fails(steve.world.signal, 3)
    asked = signal_cli.says(steve.world.signal, ASK_NAME)

    heard(steve, NAMED, after=asked)

    assert len([t for t in signal_cli.tried(steve.world.signal)
                if NAMED in t["params"].get("message", "")]) == 3
    assert not until(lambda: len(sends(steve, NAMED)) > 1, within=3)


def test_a_daemon_that_dies_is_started_again_and_answers_what_comes_after_once(steve):
    """Before, the channel reconnected every two seconds to a port nobody
    listened on, for good, while the app showed Signal ready."""
    made = steve.world
    asked = signal_cli.says(made.signal, ASK_WATER)
    heard(steve, WATERING, after=asked)
    opened = signal_cli.streams(made.signal)

    signal_cli.dies(made.signal)

    # The app's Signal page says so while it is down, and no longer once it
    # is back.
    assert until(lambda: "stopped" in (said_on_the_page(steve) or ""), within=10), steve.logs()
    assert until(lambda: signal_cli.streams(made.signal) > opened, within=60), steve.logs()
    assert until(lambda: said_on_the_page(steve) is None, within=10)
    again = signal_cli.says(made.signal, ASK_LIGHT)
    heard(steve, LIGHT, after=again)
    # What came before the daemon died is not answered a second time.
    assert not told(steve, WATERING, after=again)
    assert len(sends(steve, WATERING)) == 1
