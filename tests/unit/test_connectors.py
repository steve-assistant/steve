"""The connector slot's accounts, and calling them (`agent/connectors.py`).

Every tool of a class goes through this from step 5's item 4c: which accounts
there are, which one an id belongs to, and how all of them are asked at once.
A mistake here sends a draft's deletion to the wrong mailbox, lets one dead
server hold up every search, or puts two calls at once on Google's client,
whose replies then corrupt each other.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from agent import connectors
from agent.connectors import Connected, NoAccount
from core import db, modules
from steve_sdk import classes, ids
from steve_sdk.classes import MailProvider
from steve_sdk.errors import (
    ConnectorError,
    SignInNeeded,
    Transient,
    Unsupported,
)
from steve_sdk.manifest import parse


class Box(MailProvider):
    """A mailbox whose search calls `on_search`, and nothing else."""

    capabilities = frozenset({classes.SEND})

    def __init__(self, on_search=None, *, shareable=True):
        self.on_search = on_search or (lambda: [])
        self.shareable = shareable

    def search(self, query, *, limit, since=None):
        return self.on_search()

    def fetch(self, native_id, *, body_chars=20_000):
        return None

    def make_draft(self, draft):
        raise Unsupported("drafts")

    def send_draft(self, message):
        raise Unsupported("sending")

    def delete(self, message):
        raise Unsupported("deleting")

    def sent_items(self, *, since, limit):
        return []

    def headers(self, native_id):
        return {}


def _account(name, provider=None, *, key=None):
    return Connected(key=key or modules.new_key(), name=name, klass="mail", module="tests",
                     provider=provider or Box())


def _search(provider):
    return provider.search("", limit=10)


def test_an_id_an_account_hands_out_routes_back_to_that_account_and_no_other(monkeypatch):
    """A later call by id (read, send, delete) must reach the account that
    found the item: the same native id can exist in two accounts, and
    Outlook's EntryIDs and IMAP's UIDs are only unique within one."""
    one, two = _account("work"), _account("home")
    monkeypatch.setattr(connectors, "accounts", lambda conn, klass, **kw: [one, two])

    found, native = connectors.by_id(None, two.id("1700000000:3:INBOX"))

    assert found is two
    assert native == "1700000000:3:INBOX"
    gone = ids.make("mail", modules.new_key(), "1700000000:3:INBOX")
    with pytest.raises(NoAccount, match="not connected now"):
        connectors.by_id(None, gone)
    with pytest.raises(NoAccount, match="search again"):
        connectors.by_id(None, "19a0000000000001")


def test_all_means_every_account_and_a_name_means_that_one_or_says_which_there_are(monkeypatch):
    """`account=` is what the model passes: "all" or nothing asks every
    account, a name asks one, and a name that is not there is answered
    with the names that are, so the model can ask again rightly."""
    work, home = _account("Work"), _account("home")
    monkeypatch.setattr(connectors, "accounts", lambda conn, klass, **kw: [work, home])

    assert connectors.named(None, "mail", "all") == [work, home]
    assert connectors.named(None, "mail", None) == [work, home]
    assert connectors.named(None, "mail", "work") == [work]
    with pytest.raises(NoAccount, match="there are: Work, home"):
        connectors.named(None, "mail", "school")


def test_a_slow_account_is_named_while_the_others_answer():
    """One server that hangs must not hold up a search of every mailbox: it
    is named in the answer as not answering, and the rest are returned."""
    stuck = threading.Event()
    slow = _account("slow", Box(lambda: stuck.wait(10) and []))
    quick = _account("quick", Box(lambda: ["found"]))

    # Released inside the run: `asyncio.run` waits for its threads before it
    # returns, so released after it, the stuck one held the test for its
    # whole ten seconds.
    async def asked():
        try:
            return await connectors.ask_all([slow, quick], _search, limit_s=0.2)
        finally:
            stuck.set()

    answers = asyncio.run(asked())

    assert [a.account.name for a in answers] == ["slow", "quick"]
    assert isinstance(answers[0].error, Transient)
    assert answers[1].result == ["found"] and answers[1].error is None


def test_a_failing_account_is_named_with_its_error_and_a_providers_bug_is_contained():
    """A lapsed sign-in is said as one; an unexpected exception in a
    provider is that account's failure, not the turn's."""
    def lapsed():
        raise SignInNeeded("token revoked")

    def broken():
        raise KeyError("payload")

    answers = asyncio.run(connectors.ask_all(
        [_account("a", Box(lapsed)), _account("b", Box(broken)), _account("c")], _search))

    assert isinstance(answers[0].error, SignInNeeded)
    assert type(answers[1].error) is ConnectorError and "KeyError" in answers[1].error.detail
    assert answers[2].result == []


def test_a_providers_error_reaches_the_model_without_the_secret_it_held():
    """Its text went on whole to the model, and so to its provider: a
    library's error can quote what the call was given."""
    from core import logscrub

    held = logscrub.hold("canary-app-password")

    def leaky():
        raise ValueError(f"login failed for maya@example.com with {held}")

    [answer] = asyncio.run(connectors.ask_all([_account("a", Box(leaky))], _search))

    assert "canary" not in answer.error.detail and logscrub.MARK in answer.error.detail


def _most_at_once(provider, calls: int) -> int:
    """How many of `calls` searches on one account ran at the same time."""
    lock, running, most = threading.Lock(), [0], [0]
    gate = threading.Barrier(2, timeout=0.5)

    def search():
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        try:
            # Two that overlap meet here; one alone waits out the timeout.
            gate.wait()
        except threading.BrokenBarrierError:
            gate.reset()
        with lock:
            running[0] -= 1
        return []

    provider.on_search = search
    account = _account("google", provider, key="a1b2c3d4e5f6")
    asyncio.run(connectors.ask_all([account] * calls, _search, limit_s=30))
    return most[0]


def test_an_account_whose_client_cannot_be_shared_is_called_one_call_at_a_time():
    """Google's httplib2 client corrupts replies when two threads use it at
    once. The shareable provider overlapping in the same run shows that it
    is the turn that keeps the other apart, not the timing."""
    assert _most_at_once(Box(shareable=False), 3) == 1
    assert _most_at_once(Box(shareable=True), 2) == 2


def test_a_call_that_has_not_had_its_turn_by_its_deadline_is_never_started():
    """Giving up must mean it does not happen: a call that gave up waiting
    for its turn and then ran anyway would be a write the user was told had
    not gone."""
    holding, release = threading.Event(), threading.Event()
    ran = []

    def first():
        holding.set()
        release.wait(10)
        return ["first"]

    provider = Box(first, shareable=False)
    account = _account("google", provider)

    async def both():
        first_call = asyncio.create_task(connectors.call(account, _search))
        await asyncio.to_thread(holding.wait, 10)
        provider.on_search = lambda: ran.append("second") or []
        with pytest.raises(Transient):
            await connectors.call(account, _search, limit_s=0.2)
        release.set()
        return await first_call

    assert asyncio.run(both()) == ["first"]
    assert ran == []


def test_a_call_given_up_on_is_not_started_when_its_turn_comes_free_after():
    """The caller's limit and the turn's wait end a moment apart; a turn
    freed in that moment must not start a call that no one is waiting for."""
    given_up = threading.Event()
    given_up.set()
    ran = []
    account = _account("google", Box(lambda: ran.append(1) or [], shareable=False))

    with pytest.raises(Transient):
        connectors._run(account, _search, None, given_up)
    assert ran == []


def test_a_capability_the_provider_did_not_declare_is_refused_naming_the_account():
    """A tool asked to archive on an account that cannot is refused before
    anything is called, in words that name the account."""
    account = _account("home")
    connectors.require(account, classes.SEND)
    with pytest.raises(Unsupported, match="home cannot archive mail"):
        connectors.require(account, classes.ARCHIVE)


def test_each_kind_of_failure_is_said_once_naming_the_account():
    """The tools, the app and chat all say an account's trouble the same way."""
    assert connectors.say(SignInNeeded(), "work") == (
        "work needs you: sign in again, in Steve's app, under Settings: Accounts.")
    assert connectors.say(Transient("no answer in 60 seconds"), "home") == (
        "home can't be reached; Steve keeps trying (no answer in 60 seconds).")
    assert connectors.say(Unsupported(), "home") == "home cannot do that."


# --- the accounts a class has -----------------------------------------------------

def _manifest(module_id):
    return parse({"manifest_version": 1, "sdk": 1, "id": module_id, "label": module_id.title(),
                  "for": "Tests", "classes": ["mail"],
                  "setup": {"steps": ["address", "fields", "test"]},
                  "mail": {"engine": "imap", "imap": "imap.mail.example:993",
                           "smtp": "smtp.mail.example:465"}})


def test_the_accounts_are_those_the_user_added_by_name(tmp_path, monkeypatch):
    """The accounts table is the list, for every module that is on: an
    account of a module switched off is not asked. Today's connections stood
    in after them until step 5's item 10."""
    conn = db.open_store(tmp_path / "connectors.db")
    manifests = {"mailish": _manifest("mailish"), "otherish": _manifest("otherish")}
    modules.register(conn, manifests)
    built = []
    monkeypatch.setitem(connectors.ENGINES, "imap",
                        lambda ctx, spec: built.append(ctx.account.name) or Box())
    for module_id, name in (("mailish", "Work"), ("mailish", "Home"), ("otherish", "Club")):
        modules.add_account(conn, module_id=module_id, name=name, classes=["mail"],
                            addresses=[f"{name.lower()}@mail.example"], manifests=manifests)
    modules.set_enabled(conn, "otherish", False)

    found = connectors.accounts(conn, "mail", manifests=manifests)

    assert [a.name for a in found] == ["Home", "Work"]
    assert built == ["Home", "Work"]
    routed, native = connectors.by_id(conn, found[0].id("42"), manifests=manifests)
    assert (routed.name, routed.key, native) == ("Home", found[0].key, "42")
    conn.close()


def test_an_outlook_account_the_user_added_is_one_account_in_each_of_its_classes(tmp_path):
    """Classic Outlook stood in under a fixed key, whether or not the user
    wanted it, until step 5's item 10. Now adding it is the user's yes: it
    serves mail and calendar under the key it was given, and building it
    reaches nothing of Outlook's (the fences refuse COM)."""
    from modules.outlook_classic.provider import OutlookCalendar, OutlookMail
    from tests.fakes import outlook as fake_outlook

    conn = db.open_store(tmp_path / "connectors.db")
    assert connectors.accounts(conn, "mail") == []
    key = fake_outlook.add(conn)

    def listed(klass):
        return [(a.name, a.key, type(a.provider)) for a in connectors.accounts(conn, klass)]

    assert listed("mail") == [("outlook", key, OutlookMail)]
    assert listed("calendar") == [("outlook", key, OutlookCalendar)]
    assert listed("files") == []
    conn.close()
