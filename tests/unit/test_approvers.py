"""Setting up who may approve, from the app.

`agent/worker/approvers.py` replaced `python -m core.totp setup`, a command
the app's user does not have, and gave the app's high-risk yes a passphrase
(core/monitor.py). What must hold:
- the authenticator's secret is kept only once a code made from it comes
  back, so a QR code shown and never scanned cannot lock the user out of
  approving from the phone;
- replacing the authenticator takes the passphrase, since whoever holds the
  authenticator approves spending from the phone;
- the app's decide answer says what to ask for: the passphrase, or one to
  choose first.

The keychain is faked: the fences refuse a test's writes to the real one.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from agent import approvals, runtime
from agent.worker import approvers, routes, signin
from core import db, passphrase, secrets, totp
from core.consent import classify

WORDS = "correct horse battery"
BUY = {"amount_usd": 48.0, "merchant": "Zinc", "item": "a table for two"}
EMAIL = {"to": "hugo@example.com", "subject": "Thursday", "body": "See you then."}


class Asked:
    """The little of a Starlette request the routes read."""

    def __init__(self, body: object = None):
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.fixture
def keychain(monkeypatch) -> dict:
    kept: dict[str, str] = {}
    monkeypatch.setattr(secrets, "set_secret", lambda key, value: kept.__setitem__(key, value))
    monkeypatch.setattr(secrets, "get", lambda key, default=None: kept.get(key, default))
    monkeypatch.setattr(approvers, "_pending", {"secret": None, "until": 0.0})
    monkeypatch.setattr(signin, "_session", {"digest": None, "seen": 0.0})
    return kept


def _from_app(body: dict) -> Asked:
    """The app's request, with the session it holds once signed in: setting
    up the authenticator takes it (`signin.refused`)."""
    return Asked({**body, "session": signin.start()})


def _not_now(secret: str) -> str:
    """A code the authenticator shows at no moment near now."""
    near = {totp.code_at(secret, time.time() + 30 * k) for k in range(-3, 4)}
    return next(code for code in ("000000", "111111", "222222", "333333") if code not in near)


def _said(response) -> tuple[int, dict]:
    return response.status_code, json.loads(response.body)


# ---------------------------------------------------------------------------
# The passphrase
# ---------------------------------------------------------------------------

async def test_setup_says_which_of_the_two_is_set(keychain):
    before = _said(await approvers.setup(Asked()))
    passphrase.choose(WORDS)
    keychain[totp.SECRET_KEY] = totp.new_secret()

    after = _said(await approvers.setup(Asked()))

    assert before == (200, {"passphrase": False, "authenticator": False, "hello": False,
                            "codes_paused_until": None})
    assert after == (200, {"passphrase": True, "authenticator": True, "hello": False,
                           "codes_paused_until": None})


async def test_setup_says_until_when_codes_are_stopped_after_five_wrong_ones(keychain):
    """The phone is told to answer in the app; the app says why, and until
    when (ui/src/Approvers.tsx)."""
    for _ in range(totp.TRIES):
        totp.wrong()

    _, said = _said(await approvers.setup(Asked()))

    until = said["codes_paused_until"]
    assert until is not None and until > time.time() + totp.PAUSED_S - 60


async def test_setup_reads_the_passphrase_and_the_code_count_off_the_event_loop(
        keychain, monkeypatch):
    """Both are files in the data folder, and were read on the loop every
    request to the worker shares, beside neighbours that already went to a
    thread (step 11's review)."""
    read_in: list[int] = []

    def is_set() -> bool:
        read_in.append(threading.get_ident())
        return False

    def paused_until() -> None:
        read_in.append(threading.get_ident())

    monkeypatch.setattr(passphrase, "is_set", is_set)
    monkeypatch.setattr(totp, "paused_until", paused_until)

    await approvers.setup(Asked())

    assert len(read_in) == 2 and threading.get_ident() not in read_in


async def test_a_passphrase_is_chosen_and_changed_only_with_the_old_one(keychain):
    short = _said(await approvers.choose_passphrase(Asked({"new": "short"})))
    chosen = _said(await approvers.choose_passphrase(Asked({"new": WORDS})))
    unasked = _said(await approvers.choose_passphrase(Asked({"new": "another one entirely"})))
    changed = _said(await approvers.choose_passphrase(
        Asked({"new": "another one entirely", "old": WORDS})))

    assert short[0] == 400 and "at least 8" in short[1]["error"]
    assert chosen == (200, {"ok": True})
    assert unasked[0] == 400 and changed == (200, {"ok": True})
    assert passphrase.check("another one entirely") is None


# ---------------------------------------------------------------------------
# The authenticator
# ---------------------------------------------------------------------------

async def test_replacing_the_authenticator_takes_the_passphrase(keychain):
    passphrase.choose(WORDS)

    refused = _said(await approvers.start_authenticator(_from_app({"passphrase": "guess work"})))

    assert refused[0] == 400 and "not the passphrase" in refused[1]["error"]
    assert approvers._pending["secret"] is None and keychain == {}


async def test_the_secret_is_kept_only_once_a_code_from_it_comes_back(keychain):
    passphrase.choose(WORDS)
    status, shown = _said(await approvers.start_authenticator(_from_app({"passphrase": WORDS})))
    assert status == 200 and shown["qr"].lstrip().startswith("<") and keychain == {}

    wrong = _said(await approvers.confirm_authenticator(
        _from_app({"code": _not_now(shown["key"])})))
    right = _said(await approvers.confirm_authenticator(
        _from_app({"code": totp.code_at(shown["key"], time.time())})))

    assert wrong[0] == 400 and "does not match" in wrong[1]["error"]
    assert right == (200, {"ok": True})
    assert keychain == {totp.SECRET_KEY: shown["key"]}


async def test_a_setup_left_too_long_is_not_kept(keychain):
    passphrase.choose(WORDS)
    _, shown = _said(await approvers.start_authenticator(_from_app({"passphrase": WORDS})))
    approvers._pending["until"] = time.monotonic() - 1

    late = _said(await approvers.confirm_authenticator(
        _from_app({"code": totp.code_at(shown["key"], time.time())})))

    assert late[0] == 400 and "ran out of time" in late[1]["error"] and keychain == {}


async def test_a_confirmed_setup_cannot_be_confirmed_again(keychain):
    """Once kept, the pending secret is gone: a second confirm keeps
    nothing new."""
    passphrase.choose(WORDS)
    _, shown = _said(await approvers.start_authenticator(_from_app({"passphrase": WORDS})))
    code = totp.code_at(shown["key"], time.time())
    await approvers.confirm_authenticator(_from_app({"code": code}))

    again = _said(await approvers.confirm_authenticator(_from_app({"code": code})))

    assert again[0] == 400 and approvers._pending["secret"] is None


# ---------------------------------------------------------------------------
# The app's yes
# ---------------------------------------------------------------------------

@pytest.fixture
def cards(tmp_path, monkeypatch):
    conn = db.open_store(tmp_path / "cards.db")
    monkeypatch.setattr(runtime, "conn", conn)
    monkeypatch.setattr(approvals, "_notify", None)
    monkeypatch.setattr(signin, "_session", {"digest": None, "seen": 0.0})
    yield conn
    conn.close()


async def _card(conn, tool, args) -> int:
    receipt = await approvals.request(conn, tool=tool, args=dict(args), preview=f"{tool} card",
                                      consent=classify(tool, args), channel="tray")
    return receipt["action_id"]


async def test_a_high_risk_yes_in_the_app_asks_for_a_passphrase_to_be_chosen_first(cards):
    action_id = await _card(cards, "purchase", BUY)

    status, said = _said(await routes.action_decide(Asked({"action_id": action_id,
                                                           "approve": True})))

    assert status == 400 and said["needs"] == "new_passphrase"
    assert approvals.get(cards, action_id).status == "pending"


async def test_a_high_risk_yes_in_the_app_asks_for_the_passphrase_then_goes(cards):
    passphrase.choose(WORDS)
    session = signin.start()
    action_id = await _card(cards, "purchase", BUY)

    bare = _said(await routes.action_decide(Asked({"action_id": action_id, "approve": True,
                                                   "session": session})))
    given = _said(await routes.action_decide(Asked({"action_id": action_id, "approve": True,
                                                    "passphrase": WORDS, "session": session})))

    assert bare[0] == 400 and bare[1]["needs"] == "passphrase"
    assert given[0] == 200 and given[1]["ok"]
    assert approvals.get(cards, action_id).status == "approved"


async def test_a_high_risk_yes_to_a_card_changed_since_it_was_read_asks_for_no_passphrase(cards):
    """The passphrase is asked for, and a wrong one counted toward the lock,
    only for the card the user is looking at: a yes to one that changed
    since says so first, so it is read again before anything is typed."""
    passphrase.choose(WORDS)
    session = signin.start()
    action_id = await _card(cards, "purchase", BUY)

    status, said = _said(await routes.action_decide(Asked({
        "action_id": action_id, "approve": True, "passphrase": "not it at all",
        "session": session, "seen": "read before it changed"})))

    assert status == 400 and "changed since you read it" in said["error"]
    assert "needs" not in said
    assert approvals.get(cards, action_id).status == "pending"


async def test_an_everyday_card_and_any_no_take_a_click_alone(cards):
    email = await _card(cards, "send_email", EMAIL)
    buy = await _card(cards, "purchase", BUY)

    yes = _said(await routes.action_decide(Asked({"action_id": email, "approve": True})))
    no = _said(await routes.action_decide(Asked({"action_id": buy, "approve": False})))

    assert yes[0] == 200 and no[0] == 200
    assert approvals.get(cards, buy).status == "rejected"
