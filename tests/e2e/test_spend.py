"""Spending stops where it should, and says so where the user will see it.

Two ends to money: the month's cap the user set, and OpenAI's own balance.
At the cap, a message is answered by a sentence saying which limit, not by
a model; a routine due then is held back without a word or a model call;
and a note offers to raise the cap. With the balance spent, the reply says
to top it up, a note says so too, and the refused call is not tried again
and again: each retry on a 429 costs nothing, but a loop of them is a
worker that never answers anything else.

Each test starts its own backend: a cap reached or a balance spent stays
so for the rest of the month, and would answer any test after it.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest

from agent import caps
from core import db, knobs, paths, spend
from tests.e2e.backend import Backend, running, until
from tests.fakes.openai import ROUTINE, ROUTINE_SET, WALK, FakeOpenAI
from tests.fakes.world import world


@pytest.fixture
def steve(tmp_path) -> Iterator[Backend]:
    with (world(tmp_path / "world", openai=FakeOpenAI(WALK)) as made,
          running(made, paths.data_dir()) as backend):
        yield backend


ASKED = "Anything on today?"


def chats(steve: Backend, words: str = ASKED) -> int:
    """The model's requests that carry these words: the turn's own. The
    morning briefing writes its first lines by the model when its hour has
    passed at start, so counting every request failed in the mornings."""
    return sum(kept.path in ("/v1/chat/completions", "/v1/responses")
               and words in json.dumps(kept.body) for kept in steve.world.openai.requests)


async def test_at_the_months_cap_the_reply_says_so_a_routine_waits_and_a_note_offers_more(steve):
    said = await steve.chat("Every Monday, plan my week and email it to me.")
    assert said["text"] == ROUTINE_SET, steve.logs()
    assert steve.decide(steve.card(ROUTINE["title"]), True).json()["ok"]
    assert until(lambda: [r for r in steve.get("/control/autonomy").json()["routines"]
                          if r["status"] == "active"]), steve.logs()
    assert steve.post("/control/knobs", {"changes": {"MONTH_USD": 5}}).json()["ok"]
    with (contextlib.closing(db.open_store(steve.data / "agent.db")) as conn,
          db.transaction(conn)):
        spend.bill(conn, 5.0, model="gpt-5-mini", unit=spend.Unit("chat"))
    before = chats(steve)

    said = await steve.chat(ASKED)

    assert said["type"] == "reply", steve.logs()
    assert said["text"].startswith("I've reached this month's $5.00 limit")
    assert "Autonomy → Spending caps" in said["text"]
    # Due now, and held back: no model is asked, and nothing is said.
    due = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    with contextlib.closing(db.open_store(steve.data / "agent.db")) as conn:
        with db.transaction(conn):
            conn.execute("UPDATE routines SET next_run_at = ? WHERE status = 'active'", (due,))

        def ran() -> list[dict]:
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT result FROM jobs WHERE kind = 'routine' AND result IS NOT NULL")]

        assert until(ran), steve.logs()
        assert ran() == [{"ran": False, "why": "budget"}]
    assert chats(steve) == before
    notes = [n for n in steve.get("/control/needs").json()["needs"] if n["kind"] == "stopped"]
    assert [n["actions"] for n in notes] == [["raise_cap", "not_now"]]


async def test_with_openais_balance_spent_the_reply_says_to_top_up_and_nothing_retries(steve):
    fake, key = steve.world.openai, steve.world.secrets["OPENAI_API_KEY"]
    fake.keys.discard(key)
    fake.broke.add(key)

    said = await steve.chat(ASKED)

    assert said["type"] == "error" and caps.NO_CREDIT in said["error"], said
    asked = chats(steve)
    # OpenAI's SDK tries a 429 twice more on its own; nothing past that.
    # Five came on CI's first nightly run (22 October 2026), and the
    # assertion said nothing of which: the SDK numbers its own tries.
    assert 1 <= asked <= 3, ([(kept.path, kept.headers.get("x-stainless-retry-count"))
                              for kept in steve.world.openai.requests
                              if ASKED in json.dumps(kept.body)], steve.logs())
    time.sleep(5)
    assert chats(steve) == asked
    notes = [n for n in steve.get("/control/needs").json()["needs"] if n["kind"] == "stopped"]
    assert [n["actions"] for n in notes] == [["top_up", "not_now"]]


def quiet_away(now: datetime) -> dict[str, str]:
    """Quiet hours from an hour on, for eight hours, and both briefings
    between now and then: nothing is held, and no briefing is due."""
    return {"MORNING": (now + timedelta(minutes=30)).strftime("%H:%M"),
            "EVENING": (now + timedelta(minutes=40)).strftime("%H:%M"),
            "QUIET_FROM": (now + timedelta(hours=1)).strftime("%H:%M"),
            "QUIET_UNTIL": (now + timedelta(hours=9)).strftime("%H:%M")}


def test_with_a_dollar_of_the_month_left_the_phone_is_asked_once_to_raise_it(tmp_path):
    """Before 19 October the first word was the stop, with everything
    already paused. The ask comes while a reply can still reach a model:
    one message, on the phone and in the app's inbox, and told once."""
    knobs.write(quiet_away(datetime.now()))
    with (contextlib.closing(db.open_store(paths.data_dir() / "agent.db")) as conn,
          db.transaction(conn)):
        spend.bill(conn, 14.20, model="gpt-5-mini", unit=spend.Unit("background"))
    ask = ("This month's spending is at $14.20 of $15.00. Want to raise the cap? Reply RAISE"
           " and the new cap, like RAISE 25, or just tell me.")

    # The outbox is the message: in the app's inbox at once, and sent to
    # the phone until it goes (agent/outbox.py, tested on its own). Waiting
    # here for the phone was a minute, the outbox's retry when the first
    # try comes before Signal listens.
    with (world(tmp_path / "world", signal=True) as made,
          running(made, paths.data_dir()) as steve,
          contextlib.closing(db.open_store(steve.data / "agent.db")) as conn):

        def queued() -> list[tuple]:
            return [tuple(row) for row in conn.execute(
                "SELECT text, remote FROM outbox WHERE text LIKE '%this month%' COLLATE NOCASE")]

        assert until(queued, within=30), steve.logs()
        assert queued() == [(ask, "signal")]
        [(told,)] = conn.execute("SELECT told_at FROM spend_notices"
                                 " WHERE kind = 'asked'").fetchall()
        assert told is not None
