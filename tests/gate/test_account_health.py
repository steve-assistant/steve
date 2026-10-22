"""Each account's health: five states, one sentence each, said from one place.

On 26 September 2026 Google's sign-in lapsed at 23:37 and Gmail, Calendar
and Drive failed every minute all night in a log nobody read. Each provider
said its trouble its own way, or not at all, and the app, the tools and chat
each worded it again. Now `core/health.py` makes the sentence, and these
tests hold that nothing else says it, that it is said on the user's channel
once (after six hours down, or a day before a known end) and never at night
(05-connectors.md, section 2).
"""

from __future__ import annotations

import ast
import asyncio
import datetime as dt

import pytest

from core import clock, db, health, modules, paths
from tests.gate import repo_files

PLACE = "in Steve's app, under Settings: Accounts"
SAID_ONLY_IN_HEALTH = ("needs you:", "can't be reached", "can't work on this computer",
                       "sign-in ends in", "under Settings: Accounts")
CODE = ("agent/", "channels/", "core/", "devtools/", "modules/", "scripts/", "tools/", "voice/")


def _local(day: int, hour: int) -> dt.datetime:
    return dt.datetime(2026, 10, day, hour, 0, tzinfo=clock.local_zone())


def test_each_state_has_one_sentence_with_its_one_action():
    since = _local(14, 9).replace(minute=12)

    assert health.sentence("Work", health.WORKING) == "Work is working."
    assert health.sentence("Work", health.NEEDS_YOU, kind="sign_in") == (
        f"Work needs you: sign in again, {PLACE}.")
    assert health.sentence("Work", health.NEEDS_YOU, kind="password") == (
        f"Work needs you: enter its new password, {PLACE}.")
    assert health.sentence("Work", health.NEEDS_YOU, kind="error") == (
        f"Work needs you: check it again, {PLACE}.")
    assert health.sentence("Work", health.CANT_REACH, since=since) == (
        "Work can't be reached; Steve keeps trying since 09:12 on Wednesday 14 October.")
    assert health.sentence("Work", health.CANT_WORK_HERE,
                           detail="classic Outlook is not installed.") == (
        "Work can't work on this computer: classic Outlook is not installed.")
    assert health.sentence("Work", health.OFF) == f"Work is off, {PLACE}."


def test_an_errors_kind_puts_the_account_in_its_state():
    assert health.state_of(None) == health.WORKING
    assert health.state_of("sign_in") == health.NEEDS_YOU
    assert health.state_of("password") == health.NEEDS_YOU
    assert health.state_of("transient") == health.CANT_REACH
    assert health.state_of("not_connected") == health.CANT_WORK_HERE
    assert health.state_of("error") == health.NEEDS_YOU


def _strings(rel: str) -> list[str]:
    return [n.value for n in ast.walk(repo_files.parsed(rel))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_no_text_but_core_health_says_an_accounts_health():
    """The app, a tool's error and chat say the sentence `core/health.py`
    makes; a second wording is how a tool came to say "needs the user to
    sign in again" while nothing said where."""
    found = []
    for rel in repo_files.python(*CODE):
        if rel == "core/health.py":
            continue
        for text in _strings(rel):
            # A module's docstring may quote the sentence to argue for it.
            if text.count("\n") > 2:
                continue
            found += [f"{rel}: {text!r}" for phrase in SAID_ONLY_IN_HEALTH if phrase in text]
    assert found == []


@pytest.fixture
def conn():
    c = db.open_store(paths.db_path())
    modules.register(c)
    key = modules.add_account(c, module_id="imap", name="Work", classes=["mail"],
                              addresses=["maya@work.example"])
    yield c, key
    c.close()


def test_since_moves_only_when_the_state_does(conn):
    c, key = conn
    first = health.record(c, key, kind="transient", now=_local(14, 9))
    again = health.record(c, key, kind="transient", now=_local(14, 10))
    back = health.record(c, key, now=_local(14, 11))

    assert first.since == again.since == _local(14, 9)
    assert again.checked_at == _local(14, 10)
    assert back.state == health.WORKING and back.since == _local(14, 11)


def test_an_account_down_six_hours_is_due_once_and_working_or_off_never(conn):
    c, key = conn
    found = health.record(c, key, kind="sign_in", now=_local(14, 9))

    assert health.due(found, "Work", _local(14, 14)) is None, "five hours is not six"
    mark, said = health.due(found, "Work", _local(14, 15))
    assert said == f"Work needs you: sign in again, {PLACE}."
    health.told(c, key, mark)
    assert health.due(health.read(c, key), "Work", _local(15, 9)) is None, "said once"
    assert health.due(health.record(c, key, state=health.OFF, now=_local(14, 9)),
                      "Work", _local(16, 9)) is None


def test_a_known_end_is_due_once_a_day_before_and_not_after(conn):
    c, key = conn
    ends = _local(16, 9)
    found = health.record(c, key, expires=ends, now=_local(14, 9))

    assert health.due(found, "Work", _local(15, 8)) is None, "more than a day ahead"
    mark, said = health.due(found, "Work", _local(15, 12))
    assert said.startswith("Work's sign-in ends in about 21 hours, at 09:00 on Friday 16")
    assert said.endswith(f"sign in again, {PLACE}.")
    health.told(c, key, mark)
    assert health.due(health.read(c, key), "Work", _local(15, 13)) is None
    assert health.due(found, "Work", _local(16, 10)) is None, "past its end it is said down"


def test_steve_says_a_lapsed_sign_in_once_on_the_channel_and_never_at_night(conn):
    from agent import accounts

    c, key = conn
    health.record(c, key, kind="sign_in", now=_local(13, 18))
    sent = []

    async def notify(text):
        sent.append(text)
        return 1

    asyncio.run(accounts.tell(c, notify, now=_local(14, 3)))
    assert sent == [], "said at three in the morning"
    asyncio.run(accounts.tell(c, notify, now=_local(14, 12)))
    asyncio.run(accounts.tell(c, notify, now=_local(14, 13)))
    assert sent == [f"Work needs you: sign in again, {PLACE}."]


def test_a_notice_not_delivered_is_tried_again(conn):
    from agent import accounts

    c, key = conn
    health.record(c, key, kind="sign_in", now=_local(13, 18))
    tries = []

    async def down(text):
        tries.append(text)
        return None

    asyncio.run(accounts.tell(c, down, now=_local(14, 12)))
    asyncio.run(accounts.tell(c, down, now=_local(14, 13)))
    assert len(tries) == 2
