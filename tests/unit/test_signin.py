"""The app's sign-in and Windows Hello, on the backend's side.

The app shows nothing until it signs in, once a passphrase is set, and an
answer to a card from it needs the session it got (agent/worker/signin.py).
Windows Hello's signature can stand in for the passphrase, at sign-in and on
a high-risk yes (core/hello.py). What must hold:
- a key is registered only with the passphrase, or whatever holds the
  backend's token could register its own and approve spending;
- a signature answers one challenge, once, for what it was made for: a
  sign-in's can't approve a card, one card's can't approve another, and a
  card changed since can't be approved with it;
- a session is the app's that signed in, lapses when the app stops saying it
  is alive, and ends only for its holder;
- a forgotten passphrase is replaced only on the registered key's signature
  over a reset's own challenge;
- linking a phone or setting up the authenticator takes the sign-in too;
  unlinking does not.

Hello itself can't run here: a software RSA key, as Windows Hello makes,
signs in its place.
"""

from __future__ import annotations

import base64
import json
import threading

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from agent import approvals, runtime
from agent.worker import approvers, pairing, routes, signin
from core import db, hello, kept, knobs, monitor, passphrase, settingsfile, winsignin
from core.consent import classify
from core.sandbox import win32 as sandbox_win32

WORDS = "correct horse battery"
BUY = {"amount_usd": 48.0, "merchant": "Zinc", "item": "a table for two"}


class Asked:
    """The little of a Starlette request the routes read."""

    def __init__(self, body: object = None):
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _said(response) -> tuple[int, dict]:
    return response.status_code, json.loads(response.body)


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public(private) -> str:
    return base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()


def _sign(private, message_b64: str) -> str:
    """What Windows Hello's KeyCredential.RequestSignAsync gives."""
    signed = private.sign(base64.b64decode(message_b64), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signed).decode()


@pytest.fixture
def fresh(monkeypatch):
    # The data folder's own store, as the worker's: it is where Steve
    # remembers that a passphrase and a Hello key were set (core/kept.py).
    conn = db.open_store()
    monkeypatch.setattr(runtime, "conn", conn)
    monkeypatch.setattr(approvals, "_notify", None)
    monkeypatch.setattr(signin, "_session", {"digest": None, "seen": 0.0})
    monkeypatch.setattr(hello, "_open", {})
    yield conn
    conn.close()


async def _challenge(body: dict) -> dict:
    status, said = _said(await signin.challenge(Asked(body)))
    assert status == 200, said
    return said


async def _card(conn, tool="purchase", args=BUY) -> int:
    receipt = await approvals.request(conn, tool=tool, args=dict(args), preview=f"{tool} card",
                                      consent=classify(tool, args), channel="tray")
    return receipt["action_id"]


# ---------------------------------------------------------------------------
# Registering the key
# ---------------------------------------------------------------------------

async def test_a_hello_key_is_registered_and_forgotten_only_with_the_passphrase(fresh, key):
    passphrase.choose(WORDS)

    without = _said(await approvers.register_hello(Asked({"public_key": _public(key)})))
    wrong = _said(await approvers.register_hello(Asked({"public_key": _public(key),
                                                        "passphrase": "not it at all"})))
    assert without[0] == 400 and wrong[0] == 400 and not hello.is_set()

    kept = _said(await approvers.register_hello(Asked({"public_key": _public(key),
                                                       "passphrase": WORDS})))
    assert kept[0] == 200 and hello.is_set()
    assert _said(await approvers.setup(Asked()))[1]["hello"] is True

    assert _said(await approvers.forget_hello(Asked({"passphrase": "not it at all"})))[0] == 400
    assert hello.is_set()
    assert _said(await approvers.forget_hello(Asked({"passphrase": WORDS})))[0] == 200
    assert not hello.is_set()


async def test_only_an_rsa_key_of_hellos_size_is_taken(fresh):
    """Hello's keys are RSA-2048: anything else was not made by Hello."""
    passphrase.choose(WORDS)
    small = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    curve = ec.generate_private_key(ec.SECP256R1())

    for private in (small, curve):
        said = _said(await approvers.register_hello(Asked({"public_key": _public(private),
                                                           "passphrase": WORDS})))
        assert said[0] == 400 and "not a key Windows Hello made" in said[1]["error"]
    assert _said(await approvers.register_hello(Asked({"public_key": "not base64!",
                                                       "passphrase": WORDS})))[0] == 400
    assert not hello.is_set()


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------

async def test_no_sign_in_is_asked_until_there_is_a_passphrase_to_sign_in_with(fresh):
    assert _said(await signin.state(Asked()))[1]["required"] is False
    passphrase.choose(WORDS)
    state = _said(await signin.state(Asked()))[1]
    assert state["required"] is True and state["hello"] is False
    assert state["hello_name"].startswith("steve.") and state["hello_name"].endswith(".hello")


async def test_the_passphrase_signs_in_and_a_wrong_one_counts_as_a_try(fresh):
    passphrase.choose(WORDS)

    wrong = _said(await signin.with_passphrase(Asked({"passphrase": "not it at all"})))
    right = _said(await signin.with_passphrase(Asked({"passphrase": WORDS})))

    assert wrong[0] == 400 and "4 more tries" in wrong[1]["error"]
    assert right[0] == 200 and signin.live(right[1]["session"])


async def test_hellos_signature_over_its_challenge_signs_in_once(fresh, key):
    passphrase.choose(WORDS)
    hello.register(_public(key), WORDS)

    asked = await _challenge({"for": "signin"})
    signed = {"nonce": asked["nonce"], "signature": _sign(key, asked["message"])}
    first = _said(await signin.with_hello(Asked(signed)))
    again = _said(await signin.with_hello(Asked(signed)))

    assert first[0] == 200 and signin.live(first[1]["session"])
    assert again[0] == 400 and "ran out of time" in again[1]["error"], "a nonce is spent once"


async def test_a_signature_from_another_key_or_too_late_signs_nothing_in(fresh, key):
    passphrase.choose(WORDS)
    hello.register(_public(key), WORDS)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    asked = await _challenge({"for": "signin"})
    forged = _said(await signin.with_hello(Asked({
        "nonce": asked["nonce"], "signature": _sign(other, asked["message"])})))
    assert forged[0] == 400 and "did not match" in forged[1]["error"]

    nonce, message = hello.challenge("signin", "", now=1000.0)
    late = hello.verify(nonce, _sign(key, base64.b64encode(message).decode()), "signin", "",
                        now=1000.0 + hello.CHALLENGE_S)
    assert late is not None and "ran out of time" in late


async def test_a_session_lapses_unless_the_app_says_it_is_alive_and_ends_only_for_its_holder(
        fresh):
    passphrase.choose(WORDS)
    first = signin.start(now=1000.0)
    assert signin.live(first, now=1000.0 + signin.ALIVE_S - 1)
    assert not signin.live(first, now=1000.0 + signin.ALIVE_S), "an app that went quiet"

    first = signin.start()
    assert _said(await signin.alive(Asked({"session": first})))[1] == {
        "live": True, "sign_out_idle": False}
    second = signin.start()
    assert not signin.live(first), "signing in again replaces the session"
    assert _said(await signin.alive(Asked({"session": first})))[1] == {"live": False}

    await signin.finish(Asked({"session": first}))
    assert signin.live(second), "a stale app's end leaves the one signed in now"
    await signin.finish(Asked({"session": second}))
    assert not signin.live(second)


async def test_the_keep_alive_tells_the_app_to_sign_out_when_idle_only_once_the_user_asks(fresh):
    """The app signed out after 30 minutes away, always, until 19 October
    2026; now only once the user turns it on. The shell learns it from this
    answer each minute (ui/src-tauri/src/signin.rs `heard`)."""
    passphrase.choose(WORDS)
    session = signin.start()
    assert _said(await signin.alive(Asked({"session": session})))[1]["sign_out_idle"] is False

    knobs.write({"SIGN_OUT_IDLE": "on"})
    assert _said(await signin.alive(Asked({"session": session})))[1] == {
        "live": True, "sign_out_idle": True}


# ---------------------------------------------------------------------------
# Answering cards
# ---------------------------------------------------------------------------

async def test_an_answer_from_the_app_needs_a_live_session_once_a_passphrase_is_set(fresh):
    email = {"to": "hugo@example.com", "subject": "Thursday", "body": "See you then."}
    before = await _card(fresh, "send_email", email)
    assert _said(await routes.action_decide(Asked({"action_id": before, "approve": True})))[0] \
        == 200, "during setup, with nothing to sign in with, a click answers"

    passphrase.choose(WORDS)
    card = await _card(fresh, "send_email", email)
    bare = _said(await routes.action_decide(Asked({"action_id": card, "approve": True})))
    stale = _said(await routes.action_decide(Asked({"action_id": card, "approve": False,
                                                    "session": "made up"})))
    assert bare[0] == 400 and bare[1]["needs"] == "signin"
    assert stale[0] == 400 and stale[1]["needs"] == "signin"
    assert approvals.get(fresh, card).status == "pending"

    session = signin.start()
    said = _said(await routes.action_decide(Asked({"action_id": card, "approve": True,
                                                   "session": session})))
    assert said[0] == 200 and approvals.get(fresh, card).status == "approved"


async def test_hellos_signature_over_a_card_approves_that_card_and_no_other(fresh, key):
    passphrase.choose(WORDS)
    hello.register(_public(key), WORDS)
    session = signin.start()
    first = await _card(fresh)

    def decide(action_id: int, asked: dict):
        return routes.action_decide(Asked({
            "action_id": action_id, "approve": True, "session": session,
            "hello": {"nonce": asked["nonce"], "signature": _sign(key, asked["message"])}}))

    signed_in = await _challenge({"for": "signin"})
    assert _said(await decide(first, signed_in))[0] == 400, "a sign-in's signature"
    for_first = await _challenge({"for": "yes", "action_id": first})
    # A second card of the same tool would join the first while it waits.
    await routes.action_decide(Asked({"action_id": first, "approve": False, "session": session}))
    second = await _card(fresh)
    assert second != first
    assert _said(await decide(second, for_first))[0] == 400, "another card's signature"
    assert approvals.get(fresh, second).status == "pending"

    for_second = await _challenge({"for": "yes", "action_id": second})
    said = _said(await decide(second, for_second))
    assert said[0] == 200, said
    assert approvals.get(fresh, second).status == "approved"
    assert fresh.execute("SELECT factor FROM approval_proofs WHERE action_id = ?",
                         (second,)).fetchone()[0] == "hello"


async def test_a_card_changed_after_its_challenge_is_not_approved_with_that_signature(
        fresh, key):
    """The challenge binds the card's arguments as they were asked about."""
    passphrase.choose(WORDS)
    hello.register(_public(key), WORDS)
    card = await _card(fresh)
    asked = await _challenge({"for": "yes", "action_id": card})
    fresh.execute("UPDATE actions SET args_hash = 'changed' WHERE id = ?", (card,))
    fresh.commit()

    why = monitor.accept_hello(fresh, card, "purchase", asked["nonce"],
                               _sign(key, asked["message"]))

    assert why is not None and "something else" in why
    assert fresh.execute("SELECT COUNT(*) FROM approval_proofs").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Making a new approver
# ---------------------------------------------------------------------------

_MAKERS = {
    "/signal/link": (pairing.signal_link, {}),
    "/telegram/token": (pairing.telegram_token, {"token": "123456:abc"}),
    "/telegram/pair": (pairing.telegram_pair, {}),
    "/approvals/authenticator": (approvers.start_authenticator, {"passphrase": WORDS}),
    "/approvals/authenticator/confirm": (approvers.confirm_authenticator, {"code": "123456"}),
}


@pytest.mark.parametrize("path", sorted(_MAKERS))
async def test_linking_a_phone_or_setting_up_the_authenticator_needs_the_apps_sign_in(
        fresh, path):
    """A phone linked, or an authenticator set up, approves cards for as
    long as it stays: past the app's sign-out when Windows locks. Without
    the sign-in, whoever could read the run file's token could pair a chat
    of their own (step 11's audit)."""
    route, body = _MAKERS[path]
    passphrase.choose(WORDS)

    bare = _said(await route(Asked(body)))
    stale = _said(await route(Asked({**body, "session": "made up"})))

    assert bare[0] == 400 and bare[1].get("needs") == "signin"
    assert stale[0] == 400 and stale[1].get("needs") == "signin"


async def test_with_the_sign_in_linking_goes_on_to_its_own_checks(fresh, monkeypatch):
    monkeypatch.setattr(runtime, "telegram", None)
    passphrase.choose(WORDS)

    said = _said(await pairing.telegram_pair(Asked({"session": signin.start()})))

    assert said[1].get("needs") is None and said[1]["error"] == "Give Steve a bot first."


async def test_unlinking_a_phone_needs_no_sign_in(fresh, monkeypatch):
    """Unlinking only takes an approver away: whoever is at the laptop
    should always be able to, signed in or not."""
    monkeypatch.setattr(runtime, "telegram", None)
    passphrase.choose(WORDS)

    said = _said(await pairing.telegram_unpair(Asked()))

    assert said[0] == 200 and "goodbye" in said[1]


# ---------------------------------------------------------------------------
# Resetting a forgotten passphrase
# ---------------------------------------------------------------------------

NEW = "plum trees by the river"


async def _reset(key, new: str = NEW, purpose: str = "reset") -> tuple[int, dict]:
    asked = await _challenge({"for": purpose})
    return _said(await approvers.reset_passphrase(Asked({
        "new": new, "nonce": asked["nonce"], "signature": _sign(key, asked["message"])})))


def test_ten_wrong_guesses_made_at_once_are_each_counted_and_lock_the_passphrase():
    """The sign-in made a guess cheap: it needs only the token. Ten wrong
    guesses sent together each read a count of 0 and wrote 1, or raised on
    a shared scratch file and were never counted, and the right passphrase
    was let in after them. Each is counted, and five lock it."""
    passphrase.choose(WORDS)
    start = threading.Barrier(10)
    said: list[str] = []

    def guess(n: int) -> None:
        start.wait()
        try:
            said.append(passphrase.check(f"a wrong guess, number {n}") or "let in")
        except Exception as exc:  # noqa: BLE001
            said.append(f"raised {exc!r}")

    guessers = [threading.Thread(target=guess, args=(n,)) for n in range(10)]
    for thread in guessers:
        thread.start()
    for thread in guessers:
        thread.join()

    assert sum("not the passphrase" in why for why in said) == passphrase.TRIES, said
    assert sum("too many wrong tries" in why for why in said) == 10 - passphrase.TRIES, said
    assert "too many wrong tries" in (passphrase.check(WORDS) or "")


async def test_a_forgotten_passphrase_is_reset_with_a_hello_glance_which_lifts_its_lock(fresh, key):
    """Forgotten, the passphrase was reset only by deleting its file, which
    a user of the app may not know how to find. The registered key's
    signature stands in for the old one, as it does at a yes; the old one
    then opens nothing, and the lock its wrong guesses set is gone."""
    passphrase.choose(WORDS)
    assert hello.register(_public(key), WORDS) is None
    for _ in range(passphrase.TRIES):
        passphrase.check("not it at all")
    assert "too many wrong tries" in passphrase.check(WORDS)

    assert await _reset(key) == (200, {"ok": True})
    assert passphrase.check(NEW) is None
    assert passphrase.check(WORDS) is not None


async def test_a_reset_takes_a_signature_made_for_a_reset_by_the_registered_key_once(fresh, key):
    """Whatever holds the backend's token can ask for a challenge, but only
    the user's glance signs one. A sign-in's signature, another key's, or a
    reset's used again resets nothing."""
    passphrase.choose(WORDS)
    assert hello.register(_public(key), WORDS) is None
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    assert (await _reset(key, purpose="signin"))[0] == 400
    assert (await _reset(other))[0] == 400
    assert _said(await approvers.reset_passphrase(Asked({"new": NEW})))[0] == 400
    assert passphrase.check(WORDS) is None

    asked = await _challenge({"for": "reset"})
    body = {"new": NEW, "nonce": asked["nonce"], "signature": _sign(key, asked["message"])}
    assert _said(await approvers.reset_passphrase(Asked(body)))[0] == 200
    again = _said(await approvers.reset_passphrase(Asked({**body, "new": "another one entirely"})))
    assert again[0] == 400 and "ran out of time" in again[1]["error"]
    assert passphrase.check(NEW) is None


async def test_without_hello_a_reset_points_to_windows_sign_in_and_a_short_one_keeps_the_glance(
        fresh, key):
    """With no key, Windows' own sign-in stands in for the passphrase. A
    new passphrase too short is refused before the signature is looked at,
    so the same glance still serves once it is longer."""
    passphrase.choose(WORDS)
    without = _said(await approvers.reset_passphrase(Asked({"new": NEW, "nonce": "n",
                                                            "signature": "s"})))
    assert without[0] == 400 and "Windows sign-in" in without[1]["error"]

    assert hello.register(_public(key), WORDS) is None
    asked = await _challenge({"for": "reset"})
    signed = {"nonce": asked["nonce"], "signature": _sign(key, asked["message"])}
    short = _said(await approvers.reset_passphrase(Asked({"new": "short", **signed})))
    assert short[0] == 400 and "at least" in short[1]["error"]
    assert _said(await approvers.reset_passphrase(Asked({"new": NEW, **signed})))[0] == 200
    assert passphrase.check(NEW) is None


def test_the_worker_serves_the_reset_where_the_app_sends_it():
    """The app posts to this path (ui/src/session.ts): a route written and
    never mounted answers a user who has just glanced with a 404."""
    import agent.server as server

    assert {"/approvals/passphrase/reset", "/approvals/passphrase/reset/windows"} <= {
        getattr(r, "path", None) for r in server.app.routes}


# ---------------------------------------------------------------------------
# A passphrase file deleted or swapped (the step 11 audit, E1)
# ---------------------------------------------------------------------------

INTRUDER = "words of someone at the desk"


async def test_a_deleted_passphrase_file_locks_and_never_reads_as_no_passphrase(fresh):
    """Deleting passphrase.json at an unlocked desk turned the sign-in off
    and let whoever sat there choose a passphrase of their own, which then
    answered every high-risk re-ask."""
    assert passphrase.choose(WORDS) is None
    passphrase.path().unlink()

    assert passphrase.state() == "gone" and passphrase.is_set() and signin.required()
    assert passphrase.choose(INTRUDER) == passphrase.GONE
    assert passphrase.check(WORDS) == passphrase.GONE
    assert not passphrase.path().exists()
    status, said = _said(await signin.with_passphrase(Asked({"passphrase": INTRUDER})))
    assert status == 400 and said["error"] == passphrase.GONE
    assert _said(await signin.state(Asked()))[1]["gone"] is True


def test_a_passphrase_file_swapped_for_one_of_someone_elses_is_locked_too(fresh):
    """Writing a file that holds one's own passphrase's hash is as easy as
    deleting one."""
    assert passphrase.choose(WORDS) is None
    held = json.loads(passphrase.path().read_text(encoding="utf-8"))
    salt = bytes.fromhex(held["salt"])
    passphrase._write({**held, "hash": passphrase._hash(INTRUDER, salt, passphrase.COST).hex()})

    assert passphrase.state() == "gone"
    assert passphrase.check(INTRUDER) == passphrase.GONE


def test_a_passphrase_set_before_steve_remembered_it_is_remembered_at_its_first_read(fresh):
    """An install from before step 11 has the file and nothing kept: it is
    taken as it is, once, and from then on a deleted file locks."""
    assert passphrase.choose(WORDS) is None
    kept.drop("passphrase")
    assert kept.get("passphrase") is None

    assert passphrase.check(WORDS) is None and kept.get("passphrase") is not None
    passphrase.path().unlink()
    assert passphrase.state() == "gone"


async def test_a_hello_key_swapped_for_someone_elses_signs_no_reset(fresh, key):
    """With the passphrase file gone, a hello.json holding the intruder's
    own key would have signed the reset of the passphrase."""
    assert passphrase.choose(WORDS) is None
    assert hello.register(_public(key), WORDS) is None
    theirs = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settingsfile.save(hello.path(), {"public_key": _public(theirs)}, hello.STEPS, private=True)
    passphrase.path().unlink()

    assert not hello.is_set()
    asked = await _challenge({"for": "reset"})
    body = {"new": INTRUDER, "nonce": asked["nonce"], "signature": _sign(theirs, asked["message"])}
    status, said = _said(await approvers.reset_passphrase(Asked(body)))
    assert status == 400 and "not set up" in said["error"]
    assert passphrase.state() == "gone"


class Windows:
    """Windows' dialog and sign-in, as `core/winsignin._ask` gives them: the
    SID signed in, None for a closed dialog, or Windows' error."""

    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def __call__(self, why: str):
        self.asked.append(why)
        if isinstance(self.answer, OSError):
            raise self.answer
        return self.answer


@pytest.mark.parametrize(("answer", "error"), [
    ("S-1-5-21-1-2-3-1001", "another Windows account"),
    (None, "cancelled"),
    (OSError(None, "LogonUserW", None, 1326), "not this Windows account's password"),
    (OSError(None, "LogonUserW", None, 1327), "has no password"),
], ids=["another account", "closed", "a wrong password", "no password"])
async def test_a_reset_by_windows_sign_in_takes_this_accounts_password_and_nothing_else(
        fresh, monkeypatch, answer, error):
    """Another account's password proves nothing of this one's owner, and a
    dialog closed or a password Windows refused leaves the lock as it is."""
    assert passphrase.choose(WORDS) is None
    passphrase.path().unlink()
    windows = Windows(answer)
    monkeypatch.setattr(winsignin, "_ask", windows)

    status, said = _said(await approvers.reset_passphrase_by_windows(Asked({"new": NEW})))
    assert status == 400 and error in said["error"] and len(windows.asked) == 1
    assert passphrase.state() == "gone"


async def test_a_forgotten_passphrase_is_reset_by_this_accounts_windows_sign_in(
        fresh, monkeypatch):
    """Without Hello, a locked passphrase still has a way back for its
    owner; a passphrase too short is refused before Windows asks anything."""
    assert passphrase.choose(WORDS) is None
    passphrase.path().unlink()
    windows = Windows(sandbox_win32.user_sid())
    monkeypatch.setattr(winsignin, "_ask", windows)

    short = _said(await approvers.reset_passphrase_by_windows(Asked({"new": "short"})))
    assert short[0] == 400 and "at least" in short[1]["error"] and windows.asked == []
    assert _said(await approvers.reset_passphrase_by_windows(Asked({"new": NEW})))[0] == 200
    assert windows.asked == [approvers.PROVE_WHY]
    assert passphrase.state() == "set" and passphrase.check(NEW) is None
