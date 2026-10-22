"""Whoever holds a remote channel cannot approve a high-risk action, and the
code that decides it is the frozen core's.

Their "YES MAPLE" reaches Steve over a remote channel, and whoever holds its
device or reads its messages sees every card (docs/security-model.md). The
maintainer, 1 October 2026: for spending money, and calling anyone but them
while Steve had a phone, the yes must also carry the six digits from their
authenticator app, a secret no channel holds (`core/totp.py`,
`core/monitor.py`). Each test here
sends the words such a channel could send, through the same intercept their own messages take
(`agent/worker/answering.intercept_approval`), and checks that nothing is
approved or run without the code -- and that ordinary cards still take a
plain yes.

Also pinned: the mutable shells -- `agent/actions.act`, `agent/approvals.py`
-- defer to the monitor, so a judged change to them cannot quietly decide
for themselves.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time

import pytest

from agent import approvals, runtime
from agent.worker import answering
from channels.bridge import reaction_as_answer
from core import db, monitor, passphrase, secrets, settingsfile, totp
from core.consent import classify

SECRET = base64.b32encode(b"0123456789abcdefghij").decode().rstrip("=")
BUY = {"amount_usd": 48.0, "merchant": "Zinc", "item": "a table for two"}


@pytest.fixture
def laptop(tmp_path, monkeypatch):
    """The laptop's side: its database, an executor that only records, no
    card going anywhere, and their authenticator secret in the keychain."""
    conn = db.open_store(tmp_path / "laptop.db")
    ran: list[str] = []

    async def executor(tool, args):
        ran.append(tool)
        return {"ok": True}

    real_get = secrets.get
    monkeypatch.setattr(secrets, "get",
                        lambda key, default=None: SECRET if key == totp.SECRET_KEY
                        else real_get(key, default))
    monkeypatch.setattr(runtime, "conn", conn)
    monkeypatch.setattr(approvals, "_notify", None)
    monkeypatch.setattr(approvals, "_execute_tool", executor)
    monkeypatch.setattr(approvals, "HOLD_SECONDS", 0)
    yield {"conn": conn, "ran": ran}
    conn.close()


async def _card(conn, tool="purchase", args=BUY) -> approvals.Action:
    receipt = await approvals.request(conn, tool=tool, args=dict(args), preview=f"{tool} card",
                                      consent=classify(tool, args), channel="signal")
    assert receipt.get("action_id"), receipt
    return approvals.get(conn, receipt["action_id"])


def _code(when: float | None = None) -> str:
    return totp.code_at(SECRET, time.time() if when is None else when)


def _status(conn, action) -> str:
    return approvals.get(conn, action.id).status


async def test_a_forged_yes_without_a_code_approves_nothing(laptop):
    action = await _card(laptop["conn"])

    said = await answering.intercept_approval(f"YES {action.token}", "signal")

    assert "6-digit code" in said and _status(laptop["conn"], action) == "pending"


async def test_a_wrong_code_approves_nothing(laptop):
    action = await _card(laptop["conn"])
    wrong = str((int(_code()) + 1) % 1_000_000).zfill(6)

    said = await answering.intercept_approval(f"YES {action.token} {wrong}", "signal")

    assert "does not match" in said and _status(laptop["conn"], action) == "pending"


async def test_their_code_approves_the_card_and_it_runs(laptop):
    action = await _card(laptop["conn"])

    said = await answering.intercept_approval(f"YES {action.token} {_code()}", "signal")
    result = await approvals.execute(laptop["conn"], action.id, hold=False)

    assert said.startswith("Going ahead") and result["ok"] and laptop["ran"] == ["purchase"]
    # Recorded as their phone's: the app's yes takes the passphrase instead.
    by = laptop["conn"].execute("SELECT decided_by FROM actions WHERE id = ?",
                                (action.id,)).fetchone()[0]
    assert by == "signal"


async def test_a_code_is_good_once(laptop):
    """Seen in transit, it cannot be spent again on another card."""
    code = _code()
    first = await _card(laptop["conn"])
    await answering.intercept_approval(f"YES {first.token} {code}", "signal")
    second = await _card(laptop["conn"], args={**BUY, "merchant": "Somewhere else"})

    said = await answering.intercept_approval(f"YES {second.token} {code}", "signal")

    assert "already used" in said and _status(laptop["conn"], second) == "pending"


async def test_five_wrong_codes_stop_every_code_even_the_right_one(laptop):
    """Three codes are good at any moment, and nothing counted wrong ones:
    about 30 guesses a minute over a channel someone else holds found one
    in a day about one time in eight (step 11's audit)."""
    action = await _card(laptop["conn"])
    wrong = str((int(_code()) + 1) % 1_000_000).zfill(6)
    for _ in range(totp.TRIES):
        await answering.intercept_approval(f"YES {action.token} {wrong}", "signal")

    said = await answering.intercept_approval(f"YES {action.token} {_code()}", "signal")

    assert "answer it in the app" in said and "wrong codes" in said
    assert _status(laptop["conn"], action) == "pending"
    assert totp.paused_until() is not None, "the app is told until when"


async def test_a_code_is_checked_off_the_event_loop(laptop, monkeypatch):
    """Checking a code reads the keychain and the count's file and writes
    the database: on the loop every chat and /health share, a slow disk
    froze the worker (agent/offloop.py; step 11's review)."""
    action = await _card(laptop["conn"])
    real = monitor.accept_code
    checked_in: list[int] = []

    def accept_code(*args, **kwargs):
        checked_in.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(monitor, "accept_code", accept_code)
    await answering.intercept_approval(f"YES {action.token} {_code()}", "signal")

    assert checked_in and threading.get_ident() not in checked_in
    assert _status(laptop["conn"], action) != "pending"


async def test_guesses_sent_at_once_are_checked_one_at_a_time(laptop, monkeypatch):
    """Off the loop, guesses sent together run in threads side by side:
    each must see the count the one before it left, or more than five are
    checked before codes stop (core/totp.TRYING)."""
    action = await _card(laptop["conn"])
    real_save = totp._save

    def slow_save(count: dict) -> bool:
        time.sleep(0.05)  # a slow disk: the window another guess could slip through
        return real_save(count)

    monkeypatch.setattr(totp, "_save", slow_save)
    wrong = str((int(_code()) + 1) % 1_000_000).zfill(6)
    said = await asyncio.gather(*(answering.intercept_approval(
        f"YES {action.token} {wrong}", "signal") for _ in range(totp.TRIES + 3)))

    assert sum("does not match" in s for s in said) == totp.TRIES, said


def test_the_stop_is_counted_on_disk_and_lifts_after_fifteen_minutes(laptop):
    """A restart must not reset it: the count is in the data folder, not in
    the worker's memory."""
    conn, at = laptop["conn"], 1_800_000_000.0
    action_id = conn.execute("INSERT INTO actions (tool, args_json, args_hash, preview, reach,"
                             " reversal) VALUES ('purchase', '{}', 'h', 'p', 'outward',"
                             " 'irreversible')").lastrowid

    def yes(code: str, when: float) -> str | None:
        return monitor.accept_code(conn, action_id, "purchase", f"YES {code}", by="signal",
                                   secret=SECRET, when=when)

    wrong = str((int(_code(at)) + 1) % 1_000_000).zfill(6)
    for n in range(totp.TRIES):
        yes(wrong, at + n)
    stopped = yes(_code(at + 60), at + 60)

    assert "answer it in the app" in stopped
    assert totp.tries_path().exists()
    assert yes(_code(at + totp.PAUSED_S + 10), at + totp.PAUSED_S + 10) is None


def test_a_right_code_does_not_start_the_count_again(laptop):
    """It did, and each code the user sent bought a guesser four more tries
    (step 11's second review)."""
    conn, at = laptop["conn"], 1_800_000_000.0
    ids = [conn.execute("INSERT INTO actions (tool, args_json, args_hash, preview, reach,"
                        " reversal) VALUES ('purchase', '{}', ?, 'p', 'outward',"
                        " 'irreversible')", (f"h{n}",)).lastrowid for n in range(2)]

    def yes(action_id: int, code: str, when: float) -> str | None:
        return monitor.accept_code(conn, action_id, "purchase", f"YES {code}", by="signal",
                                   secret=SECRET, when=when)

    wrong = str((int(_code(at)) + 1) % 1_000_000).zfill(6)
    for n in range(totp.TRIES - 1):
        yes(ids[0], wrong, at + n)
    assert yes(ids[0], _code(at + 30), at + 30) is None
    stopped = yes(ids[1], _wrong(at + 60), at + 60)

    assert stopped is not None and "answer it in the app" in stopped
    assert totp.paused_until(at + 60) == at + 60 + totp.PAUSED_S


def test_wrong_codes_more_than_a_day_apart_do_not_add_up(laptop):
    """The count is of the last day's: one typo a day never stops codes."""
    conn, at = laptop["conn"], 1_800_000_000.0
    for n in range(totp.TRIES - 1):
        _yes(conn, _purchase(conn), _wrong(at + n), at + n)

    later = at + totp.DAY_S + 10
    said = _yes(conn, _purchase(conn), _wrong(later), later)

    assert said is not None and "4 more wrong" in said
    assert totp.paused_until(later) is None


def _purchase(conn) -> int:
    return conn.execute("INSERT INTO actions (tool, args_json, args_hash, preview, reach,"
                        " reversal) VALUES ('purchase', '{}', 'h', 'p', 'outward',"
                        " 'irreversible')").lastrowid


def _yes(conn, action_id: int, code: str, when: float) -> str | None:
    return monitor.accept_code(conn, action_id, "purchase", f"YES {code}", by="signal",
                               secret=SECRET, when=when)


def _wrong(at: float) -> str:
    return str((int(_code(at)) + 1) % 1_000_000).zfill(6)


@pytest.mark.parametrize("held", ["not json {", '{"wrong_at": "five"}', '{"wrong_at": ["five"]}',
                                  "[1, 2]"],
                         ids=["garbled", "not-a-list", "not-a-time", "not-an-object"])
def test_a_count_that_cannot_be_read_stops_codes_rather_than_counting_none(laptop, held):
    """A count file that could not be read counted zero wrong codes (step
    11's review): spoiling it once let every guess after through uncounted."""
    conn, at = laptop["conn"], 1_800_000_000.0
    totp.tries_path().parent.mkdir(parents=True, exist_ok=True)
    totp.tries_path().write_text(held, encoding="utf-8")

    stopped = _yes(conn, _purchase(conn), _code(at), at)

    assert stopped is not None and "answer it in the app" in stopped
    assert totp.paused_until(at) == at + totp.PAUSED_S, "the app is told until when"
    assert totp.paused_until(at + totp.PAUSED_S + 1) == at + 2 * totp.PAUSED_S + 1, (
        "while it still cannot be read, codes stay stopped")


def test_a_wrong_code_that_cannot_be_counted_stops_codes_and_raises_nothing(
        laptop, monkeypatch):
    """A write that failed -- a full disk, a file held -- raised after the
    guess was checked, so each guess went uncounted and the reply was an
    error (step 11's review)."""
    conn, at = laptop["conn"], 1_800_000_000.0

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(settingsfile, "save", full_disk)
    action_id = _purchase(conn)

    said = _yes(conn, action_id, _wrong(at), at)

    assert said is not None and "answer it in the app" in said
    assert _yes(conn, action_id, _code(at + 30), at + 30) is not None, (
        "the right code is not taken either, until the stop is over")
    assert totp.paused_until(at + 30) == at + totp.PAUSED_S


def test_a_count_a_newer_steve_wrote_stops_codes_rather_than_being_misread(laptop):
    """A newer release may keep the count another way: read as this one's,
    its wrong codes counted none (step 11's second review)."""
    conn, at = laptop["conn"], 1_800_000_000.0
    totp.tries_path().parent.mkdir(parents=True, exist_ok=True)
    totp.tries_path().write_text('{"version": 99, "wrong": {"times": [1, 2, 3, 4]}}',
                                 encoding="utf-8")

    stopped = _yes(conn, _purchase(conn), _code(at), at)

    assert stopped is not None and "answer it in the app" in stopped
    assert totp.paused_until(at) == at + totp.PAUSED_S


# The count file's keys at each of its versions (`totp.STEPS`). A change of
# keys is a step to a new version: `failed` became `wrong_at` with none, and
# each Steve read the other's count as no wrong codes (step 11's third
# review).
_COUNT_KEYS = {1: {"failed", "paused_until", "pause_s"},
               2: {"wrong_at", "paused_until", "pause_s"}}


def test_the_count_files_keys_are_those_of_its_version(laptop):
    totp.wrong(now=1_800_000_000.0)

    held = json.loads(totp.tries_path().read_text(encoding="utf-8"))

    assert held["version"] == len(totp.STEPS)
    assert set(held) - {"version"} == _COUNT_KEYS[held["version"]]


def test_wrong_codes_counted_in_a_row_before_the_update_still_count(laptop):
    """The update read four wrong codes as none, and a guesser had five
    more (step 11's third review)."""
    totp.tries_path().parent.mkdir(parents=True, exist_ok=True)
    totp.tries_path().write_text('{"version": 1, "failed": 4, "paused_until": 0, "pause_s": 0}',
                                 encoding="utf-8")

    said = totp.wrong()

    assert "answer it in the app" in said, said
    assert json.loads(totp.tries_path().read_text(encoding="utf-8"))["version"] == 2


@pytest.mark.parametrize("digits", ["\u0664\u0668\u0662\u0669\u0661\u0663",
                                    "\u09ea\u09ee\u09e8\u09ef\u09e7\u09e9"],
                         ids=["arabic-indic", "bengali"])
def test_a_code_in_another_scripts_digits_is_no_code_and_raises_nothing(laptop, digits):
    """`\\d` took them, and `hmac.compare_digest` raised a TypeError on
    them (step 11's third review)."""
    conn, at = laptop["conn"], 1_800_000_000.0

    said = _yes(conn, _purchase(conn), digits, at)

    assert said is not None and "code" in said
    assert monitor.code_in(f"YES MAPLE {digits}") is None
    assert totp.matching_step(SECRET, digits, at) is None


def test_a_code_in_full_width_digits_is_the_code(laptop):
    conn, at = laptop["conn"], 1_800_000_000.0
    wide = "".join(chr(0xFF10 + int(d)) for d in _code(at))

    assert monitor.code_in(f"YES MAPLE {wide}") == _code(at)
    assert _yes(conn, _purchase(conn), wide, at) is None


def test_each_stop_soon_after_the_last_lasts_twice_as_long_up_to_a_day(laptop):
    """Fifteen minutes at a time still let 480 guesses a day through, a good
    code found about one time in sixty a day: a guesser that keeps on
    must be slowed more each time (step 11's review)."""
    conn, at = laptop["conn"], 1_800_000_000.0
    action_id = _purchase(conn)
    pauses = []
    for _ in range(9):
        for n in range(totp.TRIES):
            _yes(conn, action_id, _wrong(at + n), at + n)
        stopped_at = at + totp.TRIES - 1
        until = totp.paused_until(stopped_at)
        pauses.append(until - stopped_at)
        at = until + 60

    assert pauses == [totp.PAUSED_S * 2 ** k for k in range(7)] + [totp.DAY_S] * 2
    assert "24 hours" in totp.paused(at - 60 - totp.DAY_S)

    at += totp.DAY_S
    for n in range(totp.TRIES):
        _yes(conn, action_id, _wrong(at + n), at + n)
    assert totp.paused_until(at + totp.TRIES - 1) == at + totp.TRIES - 1 + totp.PAUSED_S, (
        "a day without a stop starts again at fifteen minutes")


def test_a_right_code_between_stops_does_not_shorten_the_next(laptop):
    """The user's own code, sent between a guesser's stops, must not hand
    the guesser back the shortest stop."""
    conn, at = laptop["conn"], 1_800_000_000.0
    for n in range(totp.TRIES):
        _yes(conn, _purchase(conn), _wrong(at + n), at + n)
    at = totp.paused_until(at + totp.TRIES - 1) + 60
    _yes(conn, _purchase(conn), _wrong(at), at)
    assert _yes(conn, _purchase(conn), _code(at + 1), at + 1) is None

    # With the one before the right code, five within a day.
    for n in range(totp.TRIES - 1):
        _yes(conn, _purchase(conn), _wrong(at + 60 + n), at + 60 + n)

    stopped_at = at + 60 + totp.TRIES - 2
    assert totp.paused_until(stopped_at) == stopped_at + 2 * totp.PAUSED_S


async def test_an_old_code_is_refused(laptop):
    action = await _card(laptop["conn"])

    said = await answering.intercept_approval(
        f"YES {action.token} {_code(time.time() - 300)}", "signal")

    assert "does not match" in said and _status(laptop["conn"], action) == "pending"


async def test_one_high_risk_card_is_open_at_a_time(laptop):
    """So a channel holding their code has no second card to point it at."""
    await _card(laptop["conn"])

    receipt = await approvals.request(laptop["conn"], tool="purchase",
                                      args={**BUY, "merchant": "Other"}, preview="second",
                                      consent=classify("purchase", BUY), channel="signal")

    assert receipt["status"] == "refused" and "still waiting" in receipt["error"]


async def test_an_approval_that_skipped_the_code_neither_decides_nor_runs(laptop):
    """Two checks, so a change to the parsing between them cannot let one
    through: recording the yes, and running the action."""
    conn = laptop["conn"]
    action = await _card(conn)

    recorded = approvals.decide(conn, action.id, approve=True, by="signal")
    with conn:  # as if something wrote the yes behind the monitor's back
        conn.execute("UPDATE actions SET status = 'approved', decided_by = 'signal'"
                     " WHERE id = ?", (action.id,))
    ran = await approvals.execute(conn, action.id, hold=False)

    assert not recorded["ok"] and "6-digit code" in recorded["error"]
    assert not ran["ok"] and laptop["ran"] == []


async def test_in_the_app_a_high_risk_yes_takes_the_passphrase(laptop):
    """Until step 4 the app approved spending with a click: it was "this
    laptop". Whoever sits at an unlocked laptop, or drives its window, can
    click; only the user knows the passphrase."""
    conn = laptop["conn"]
    action = await _card(conn)
    assert passphrase.choose("correct horse battery") is None

    clicked = approvals.decide(conn, action.id, approve=True, by="tray")
    wrong = monitor.accept_passphrase(conn, action.id, action.tool, "wrong horse battery")
    right = monitor.accept_passphrase(conn, action.id, action.tool, "correct horse battery")
    recorded = approvals.decide(conn, action.id, approve=True, by="tray")
    result = await approvals.execute(conn, action.id, hold=False)

    assert not clicked["ok"] and "passphrase" in clicked["error"]
    assert "not the passphrase" in wrong and right is None
    assert recorded["ok"] and result["ok"] and laptop["ran"] == ["purchase"]


async def test_an_ordinary_card_still_takes_a_plain_yes(laptop):
    action = await _card(laptop["conn"], tool="send_email",
                         args={"to": "hugo@example.com", "subject": "s", "body": "b"})

    said = await answering.intercept_approval(f"YES {action.token}", "signal")

    assert said.startswith("Going ahead") and _status(laptop["conn"], action) == "approved"


async def test_a_thumbs_up_cannot_answer_a_high_risk_card(laptop):
    """A reaction carries no code, so the card's wording gives it nothing to
    match (`channels/bridge.CARD_ANSWER`)."""
    action = await _card(laptop["conn"])

    assert reaction_as_answer("👍", approvals.card(action)) is None
    assert "authenticator" in approvals.card(action)


@pytest.mark.parametrize("when, eight", [
    (59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
    (1234567890, "89005924"), (2000000000, "69279037"),
])
def test_the_codes_are_rfc_6238s(when, eight):
    """RFC 6238 Appendix B (SHA-1). The codes their app makes are these."""
    key = b"12345678901234567890"

    assert totp.hotp(key, totp.step_at(when), digits=8) == eight
    assert totp.code_at(base64.b32encode(key).decode(), when) == eight[-6:]


def test_spending_money_is_the_high_risk_tier():
    from core.consent import high_risk

    assert high_risk("purchase")
    assert not high_risk("send_email") and not high_risk("create_event")


async def test_what_act_does_is_the_monitors_decision(laptop, monkeypatch):
    """The gate's answer is carried out, not second-guessed: a refusal from
    the monitor refuses even a free tool."""
    from agent import actions
    from agent.turn import Deps

    monkeypatch.setattr(monitor, "gate", lambda conn, tool, args, **kw: monitor.Gate(
        "refused", classify(tool, args), "the monitor said no"))

    said = await actions.act(type("C", (), {"deps": Deps(conn=laptop["conn"], session_id=1)})(),
                             "remind_me", {"text": "x"}, "preview")

    assert said["ok"] is False and "the monitor said no" in said["error"]
