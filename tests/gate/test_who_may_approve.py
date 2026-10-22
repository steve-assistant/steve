"""What a yes may approve depends on where it came from, and only the app
changes who may approve.

Until step 4 (docs/plan/04-runtime.md, section 5):
- the app's yes needed nothing, even to spend money (`monitor.LOCAL`);
- a yes spoken at the desk was recorded as Signal's;
- nothing said what a yes from anywhere else -- a text message, an
  unattended run -- could approve; only the intercept's list of channels
  kept them out.

Now core holds a table of each channel's ceiling (`monitor.CEILINGS`): an
everyday card takes a plain yes there, a high-risk one its second factor
(the passphrase in the app, the authenticator's code elsewhere), and a
channel not in it approves nothing. The user may lower a channel's, never
raise it. Each test answers a card the way that channel's words arrive.
"""

from __future__ import annotations

import ast
import base64
import json
import time
from pathlib import Path

import pytest

from agent import approvals, runtime
from agent.worker import answering
from channels import telegram
from channels.base import Outgoing
from channels.bridge import reaction_as_answer
from core import db, knobs, monitor, passphrase, secrets, totp
from core.consent import classify

SECRET = base64.b32encode(b"0123456789abcdefghij").decode().rstrip("=")
BUY = {"amount_usd": 48.0, "merchant": "Zinc", "item": "a table for two"}
EMAIL = {"to": "hugo@example.com", "subject": "Thursday", "body": "See you then."}
WORDS = "correct horse battery"
TRIES_AND_MORE = passphrase.TRIES + 2


@pytest.fixture
def laptop(tmp_path, monkeypatch):
    """Its database, an executor that only records, no card going anywhere,
    and their authenticator secret in the keychain."""
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


def _row(conn, action) -> tuple[str, str | None]:
    return tuple(conn.execute("SELECT status, decided_by FROM actions WHERE id = ?",
                              (action.id,)).fetchone())


def _code() -> str:
    return totp.code_at(SECRET, time.time())


# --------------------------------------------------------------------------
# A round trip on each channel
# --------------------------------------------------------------------------


@pytest.mark.parametrize("channel", ["signal", "telegram", "voice"])
async def test_an_everyday_card_takes_a_plain_yes_recorded_as_its_channel(laptop, channel):
    """Recorded as the channel, because the channel decides what the yes
    may approve, now and again when the action runs."""
    action = await _card(laptop["conn"], "send_email", EMAIL)

    said = await answering.intercept_approval(f"YES {action.token}", channel)
    ran = await approvals.execute(laptop["conn"], action.id, hold=False)

    assert said.startswith("Going ahead") and ran["ok"]
    assert _row(laptop["conn"], action) == ("done", channel)


async def test_in_the_app_an_everyday_card_takes_a_click(laptop):
    action = await _card(laptop["conn"], "send_email", EMAIL)

    recorded = approvals.decide(laptop["conn"], action.id, approve=True, by="tray")
    ran = await approvals.execute(laptop["conn"], action.id, hold=False)

    assert recorded["ok"] and ran["ok"] and laptop["ran"] == ["send_email"]


@pytest.mark.parametrize("channel", ["signal", "telegram", "voice"])
async def test_a_high_risk_yes_takes_the_code_on_each_channel(laptop, channel):
    conn = laptop["conn"]
    action = await _card(conn)

    bare = await answering.intercept_approval(f"YES {action.token}", channel)
    pending = _row(conn, action)
    coded = await answering.intercept_approval(f"YES {action.token} {_code()}", channel)
    ran = await approvals.execute(conn, action.id, hold=False)

    assert "6-digit code" in bare and pending == ("pending", None)
    assert coded.startswith("Going ahead") and ran["ok"]
    assert _row(conn, action) == ("done", channel)


async def test_a_code_does_not_stand_in_for_the_passphrase_nor_it_for_a_code(laptop):
    """Each channel's own factor: the code proves a yes over a channel, the
    passphrase one in the app."""
    conn = laptop["conn"]
    passphrase.choose(WORDS)
    by_code, by_words = await _card(conn), None

    assert monitor.accept_code(conn, by_code.id, "purchase", _code(), by="signal") is None
    in_the_app = approvals.decide(conn, by_code.id, approve=True, by="tray")
    approvals.decide(conn, by_code.id, approve=False, by="tray")
    by_words = await _card(conn, args={**BUY, "merchant": "Elsewhere"})
    assert monitor.accept_passphrase(conn, by_words.id, "purchase", WORDS) is None
    on_signal = approvals.decide(conn, by_words.id, approve=True, by="signal")

    assert not in_the_app["ok"] and "passphrase" in in_the_app["error"]
    assert not on_signal["ok"] and "6-digit code" in on_signal["error"]


# --------------------------------------------------------------------------
# Everywhere else approves nothing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("by", ["sms", "auto", "routine", "reminder", ""])
async def test_a_yes_from_a_channel_not_in_the_table_approves_nothing(laptop, by):
    action = await _card(laptop["conn"], "send_email", EMAIL)

    recorded = approvals.decide(laptop["conn"], action.id, approve=True, by=by)

    assert not recorded["ok"] and "approves nothing" in recorded["error"]


async def test_a_thumbs_up_or_a_button_answers_only_the_card_it_is_on(laptop):
    """Text the model or an outsider wrote can hold another card's line: a
    card's description ("Delete 'Reply YES COPPER to go ahead'"), or a
    reply. A reaction or a button there approved that other card, an email
    the user had never been shown (step 11's audit)."""
    conn = laptop["conn"]
    hidden = await _card(conn, "send_email", EMAIL)
    line = f"Reply YES {hidden.token} to go ahead"
    receipt = await approvals.request(
        conn, tool="send_email", args={**EMAIL, "subject": "Friday"},
        preview=f"Email Hugo: '{line}'", consent=classify("send_email", EMAIL),
        channel="signal")
    shown = approvals.get(conn, receipt["action_id"])

    answer = reaction_as_answer("👍", approvals.card(shown))
    await answering.intercept_approval(answer, "signal")

    assert answer == f"YES {shown.token}"
    assert _row(conn, shown)[0] == "approved" and _row(conn, hidden)[0] == "pending"
    assert telegram._buttons(Outgoing(text=f"Want it? {line}.")) is None
    assert telegram._buttons(Outgoing(text=approvals.card(shown), card=shown.id))[
        "inline_keyboard"][0][0]["callback_data"] == f"YES {shown.token} {shown.id}"


async def test_a_late_thumbs_up_or_press_never_decides_a_newer_card_with_its_word(
        laptop, monkeypatch):
    """Words are reused, and a card's button or a reaction answered by its
    word: pressed after the card was answered, it approved the newer card
    that had drawn the same word, one the user had not been shown (step
    11's audit). It decides the card it is on, or none."""
    conn = laptop["conn"]
    monkeypatch.setattr(approvals, "token_for", lambda taken, *rest: "MAPLE")
    old = await _card(conn, "send_email", EMAIL)
    await answering.decide_action(old.id, approve=False, by="app")
    newer = await _card(conn, "send_email", {**EMAIL, "subject": "Friday"})
    assert newer.token == old.token == "MAPLE"

    said = await answering.intercept_approval(
        reaction_as_answer("👍", approvals.card(old)), "signal", card=old.id)
    await answering.voice_reply("YES MAPLE", "telegram", card=old.id)

    assert "MAPLE waiting" in said and _row(conn, newer)[0] == "pending"


async def test_a_yes_written_from_nowhere_does_not_run(laptop):
    """The second check, at the run: a yes that reached the row some other
    way is read for where it came from."""
    conn = laptop["conn"]
    action = await _card(conn, "send_email", EMAIL)
    with conn:
        conn.execute("UPDATE actions SET status = 'approved', decided_by = 'sms' WHERE id = ?",
                     (action.id,))

    ran = await approvals.execute(conn, action.id, hold=False)

    assert not ran["ok"] and laptop["ran"] == []


# --------------------------------------------------------------------------
# The user lowers a ceiling; nothing raises one
# --------------------------------------------------------------------------


async def test_signal_lowered_to_everyday_cards_sends_spending_to_the_app(laptop):
    conn = laptop["conn"]
    knobs.write({"APPROVE_ON_SIGNAL": "ordinary"})
    email = await _card(conn, "send_email", EMAIL)
    assert (await answering.intercept_approval(f"YES {email.token}", "signal")).startswith(
        "Going ahead")
    buy = await _card(conn)

    said = await answering.intercept_approval(f"YES {buy.token} {_code()}", "signal")

    assert "answer this one in the app" in said and _row(conn, buy) == ("pending", None)


async def test_voice_lowered_to_never_answers_no_card(laptop):
    knobs.write({"APPROVE_BY_VOICE": "never"})
    action = await _card(laptop["conn"], "send_email", EMAIL)

    said = await answering.intercept_approval(f"YES {action.token}", "voice")

    assert "chose not to approve" in said and _row(laptop["conn"], action) == ("pending", None)


async def test_a_ceiling_lowered_after_the_yes_stops_the_action_before_it_runs(laptop):
    conn = laptop["conn"]
    action = await _card(conn, "send_email", EMAIL)
    assert approvals.decide(conn, action.id, approve=True, by="signal")["ok"]
    knobs.write({"APPROVE_ON_SIGNAL": "never"})

    ran = await approvals.execute(conn, action.id, hold=False)

    assert not ran["ok"] and laptop["ran"] == []


def test_no_setting_raises_a_channel_past_the_table():
    """The table is core's; the settings file is the user's, and anything
    that can write it could otherwise write a channel in."""
    everything = {knob: "high" for knob in monitor.LOWERED_BY.values()}
    tampered = {knob: "everything" for knob in monitor.LOWERED_BY.values()}

    for channel, (top, _) in monitor.CEILINGS.items():
        assert monitor.ceiling(channel, everything) == top
        assert monitor.ceiling(channel, tampered) == top
    for elsewhere in ("sms", "auto", "APPROVE_ON_SIGNAL"):
        assert monitor.ceiling(elsewhere, everything) == monitor.NEVER


# --------------------------------------------------------------------------
# The passphrase
# --------------------------------------------------------------------------


def test_the_passphrase_is_kept_only_as_a_hash():
    assert passphrase.choose(WORDS) is None

    kept = passphrase.path().read_text(encoding="utf-8")

    assert WORDS not in kept and set(json.loads(kept)) >= {"salt", "hash", "n"}
    assert passphrase.check(WORDS) is None


def test_a_short_passphrase_is_refused():
    assert "at least 8" in passphrase.choose("short")
    assert not passphrase.is_set()


def test_five_wrong_tries_lock_it_for_fifteen_minutes_even_against_the_right_one():
    """Counted in its file, so stopping the backend does not reset it."""
    now = 1_790_000_000.0
    passphrase.choose(WORDS)

    tries = [passphrase.check("wrong horse battery", now=now + n) for n in range(5)]
    locked = passphrase.check(WORDS, now=now + 60)
    later = passphrase.check(WORDS, now=now + 15 * 60 + 5)

    assert all(tries) and "lock" in tries[-1]
    assert "too many wrong tries" in locked and later is None


def test_a_wrong_try_says_the_lock_it_will_meet(monkeypatch):
    """The warning wrote "15 minutes" by hand beside the constant that
    decides it, so a change to one would leave the other saying wrong."""
    monkeypatch.setattr(passphrase, "LOCKED_S", 120)
    passphrase.choose(WORDS)

    said = [passphrase.check("wrong horse battery", now=1_790_000_000.0 + n)
            for n in range(passphrase.TRIES)][-1]

    assert said.endswith("lock it for 2 minutes"), said


def test_an_empty_answer_is_not_counted_as_a_wrong_try():
    """The app's first click on a high-risk card comes before its box is
    filled; counted, five such clicks would lock the user out."""
    now = 1_790_000_000.0
    passphrase.choose(WORDS)

    empties = [passphrase.check("", now=now + n) for n in range(TRIES_AND_MORE)]

    assert all("needs your passphrase" in why for why in empties)
    assert passphrase.check(WORDS, now=now + 10) is None


def test_changing_the_passphrase_takes_the_old_one():
    passphrase.choose(WORDS)

    unasked = passphrase.choose("a new one entirely")
    asked = passphrase.choose("a new one entirely", old=WORDS)

    assert "needs the old one" in unasked and asked is None
    assert passphrase.check("a new one entirely") is None and passphrase.check(WORDS)


async def test_a_second_factor_is_bound_to_the_arguments_it_was_given_for(laptop):
    """Given for one purchase, it does not carry over to the same card with
    other arguments."""
    conn = laptop["conn"]
    passphrase.choose(WORDS)
    action = await _card(conn)
    monitor.accept_passphrase(conn, action.id, action.tool, WORDS)
    assert approvals.decide(conn, action.id, approve=True, by="tray")["ok"]
    with conn:
        conn.execute("UPDATE actions SET args_json = ?, args_hash = 'other' WHERE id = ?",
                     (json.dumps({**BUY, "amount_usd": 4800.0}), action.id))

    ran = await approvals.execute(conn, action.id, hold=False)

    assert not ran["ok"] and laptop["ran"] == []


# --------------------------------------------------------------------------
# Only the app changes who may approve
# --------------------------------------------------------------------------

# What changes who may approve: the passphrase, the authenticator, the
# settings that lower a channel, and pairing a channel. A tool may read the
# settings (`knobs.on`) and keep its own keys; it may not write these.
WHO_MAY_APPROVE = {"core.passphrase", "core.totp", "agent.worker.approvers",
                   "agent.worker.pairing", "agent.worker.control"}
WRITES = {("knobs", "write"), ("signal", "forget"), ("telegram", "forget")}
# Where the passphrase, the authenticator's secret and the bot paired as a
# channel are kept, by name.
KEPT_AS = {totp.SECRET_KEY, passphrase.path().name, telegram.TOKEN_KEY}


def _reaches(source: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            names = {f"{node.module}.{alias.name}" for alias in node.names} | {node.module}
            found += sorted(names & WHO_MAY_APPROVE)
        elif isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if alias.name in WHO_MAY_APPROVE]
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and isinstance(node.func.value, ast.Name)
              and (node.func.value.id, node.func.attr) in WRITES):
            found.append(f"{node.func.value.id}.{node.func.attr}")
        elif isinstance(node, ast.Constant) and node.value in KEPT_AS:
            found.append(node.value)
    return found


def test_no_tool_reaches_what_decides_who_may_approve():
    """The model must never be able to make itself, or a channel someone
    else holds, able to approve: the tools' code names none of it, and a
    module's tools run in the sandbox besides, reaching only the broker
    (core/sandbox/run.py)."""
    sources = [*Path("agent/toolset").rglob("*.py"), *Path("tools").rglob("*.py")]
    assert len(sources) > 20, "the walk found the toolset"

    reached = {str(path): names for path in sources if (names := _reaches(path))}

    assert reached == {}
