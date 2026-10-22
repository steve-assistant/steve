"""A sandboxed module's reads of the user's accounts (agent/modreads.py).

The broker lets a module read the classes it was granted; this reader does
the reading. A module that got a provider's own ids could hand them to a
host of its own, one that got an id of another class through could call a
mailbox's method on a calendar, and one that read a crafted mail unscreened
could be steered by its sender.
"""

from __future__ import annotations

import pytest

from agent import connectors, modreads, screening
from agent.connectors import Connected
from core import screen
from core.modctx import Refused
from steve_sdk import calls, ids
from steve_sdk.calls import READS
from steve_sdk.classes import Message
from steve_sdk.errors import NotConnected, SignInNeeded


class Mailbox:
    """A mailbox that keeps what it was asked and answers with `messages`."""

    shareable = True

    def __init__(self, *messages, fail=None):
        self.messages = list(messages)
        self.asked = []
        self.fail = fail

    def search(self, query, *, limit, since=None):
        self.asked.append(("search", query, limit, since))
        if self.fail:
            raise self.fail
        return self.messages[:limit]

    def fetch(self, native_id, *, body_chars=20_000):
        self.asked.append(("fetch", native_id))
        return next((m for m in self.messages if m.native_id == native_id), None)


def _message(native="n-1", **kw):
    return Message(native_id=native, ident="<n1@sender.example>", thread="t-1",
                   sender="Ana <ana@sender.example>", subject="Rent", snippet="due friday",
                   body="The rent is due on Friday.", when=1760000000000, folder="INBOX",
                   condition="etag-1", extra={"List-Id": "x"}, **kw)


def _account(name, box, key):
    return Connected(key=key, name=name, klass="mail", module="tests", provider=box)


@pytest.fixture
def two(monkeypatch):
    work = _account("Work", Mailbox(_message()), "acctwork01")
    home = _account("Home", Mailbox(_message("n-2")), "accthome01")
    monkeypatch.setattr(connectors, "accounts", lambda conn, klass, **kw: [work, home])
    return work, home


def test_every_read_the_broker_lets_through_is_one_the_reader_makes():
    """A read in the broker's table with no call here would fail as a bug
    in the module's hands; a call here the broker never allows is dead."""
    assert set(modreads._CALLS) == {(k, m) for k, methods in READS.items() for m in methods}


class Anything:
    """A provider that takes any call and finds nothing."""

    def __getattr__(self, name):
        return lambda *args, **kw: []


def test_every_read_runs_given_only_what_it_must_be_given():
    """What a module leaves out comes from the SDK's `DEFAULTS`, which its
    own tests' ctx uses too; a default missing there would fail the read in
    the module's hands and never in its tests."""
    for (klass, method), call in modreads._CALLS.items():
        needed = {name: "x" for name, kind in READS[klass][method].items()
                  if kind.endswith("!")}
        call(Anything(), {**calls.DEFAULTS.get(f"{klass}.{method}", {}), **needed},
             {"id": "n-1"})
    for read, given in calls.DEFAULTS.items():
        klass, method = read.split(".")
        assert set(given) <= set(READS[klass][method]), read


def test_a_search_given_no_limit_asks_the_provider_for_twenty(two):
    work, _ = two
    modreads.reader(None)("mail", "search", {}, None)
    assert work.provider.asked == [("search", "", 20, None)]


def test_a_module_is_given_steves_ids_and_the_account_never_the_providers_own(two):
    """A provider's ids (an Outlook EntryID, a Gmail id, a Message-ID) are
    what a module could take elsewhere and use; Steve's route back only
    through Steve. A write's condition and the extra headers stay too."""
    work, home = two
    found = modreads.reader(None)("mail", "search", {"query": "rent", "limit": 5}, None)
    assert [m["account"] for m in found] == ["Work", "Home"]
    first = found[0]
    assert first["id"] == work.id("n-1") and first["thread"] == work.id("t-1")
    assert {"native_id", "ident", "condition", "extra"}.isdisjoint(first)
    assert first["subject"] == "Rent" and first["when"] == 1760000000000
    assert work.provider.asked == [("search", "rent", 5, None)]


def test_an_id_reads_from_the_account_that_handed_it_out_and_no_other(two):
    work, home = two
    found = modreads.reader(None)("mail", "fetch", {"id": home.id("n-2")}, None)
    assert [m["id"] for m in found] == [home.id("n-2")]
    assert work.provider.asked == [] and home.provider.asked == [("fetch", "n-2")]


def test_an_id_of_another_class_or_one_steve_did_not_make_is_refused(two):
    read = modreads.reader(None)
    for bad in (ids.make("calendar", "acctwork01", "n-1"), "n-1", "mail:acctwork01:%%"):
        with pytest.raises(Refused, match="not the id of a mail item"):
            read("mail", "fetch", {"id": bad}, None)
    with pytest.raises(NotConnected, match="not connected now"):
        read("mail", "fetch", {"id": ids.make("mail", "acctgone01", "n-1")}, None)
    with pytest.raises(NotConnected, match="there are: Work, Home"):
        read("mail", "search", {}, "School")
    assert two[0].provider.asked == [] and two[1].provider.asked == []


def test_an_item_the_screen_did_not_pass_keeps_only_what_its_sender_did_not_write(
        two, monkeypatch):
    """The module may hand what it reads to a model of its own; a mail
    written to steer one must reach it as the screen allows, and its sender,
    subject and body not at all."""
    asked = []

    def withhold(conn, artifact):
        asked.append(artifact)
        return screen.Shown("[withheld: aimed at an AI]", "withheld")

    monkeypatch.setattr(screening, "_read", withhold)
    work, _ = two
    first = modreads.reader(None)("mail", "fetch", {"id": work.id("n-1")}, None)[0]
    assert asked[0].source == "email_body" and asked[0].origin == "Ana <ana@sender.example>"
    assert "The rent is due on Friday." in asked[0].text
    assert first == {"id": work.id("n-1"), "account": "Work", "thread": work.id("t-1"),
                     "when": 1760000000000, "body_length": 0, "unread": False, "labels": [],
                     "folder": "INBOX", "direction": "", "draft": False,
                     "screened": "withheld", "text": "[withheld: aimed at an AI]"}


def test_one_account_failing_leaves_the_others_and_all_failing_is_said(monkeypatch):
    ok = _account("Work", Mailbox(_message()), "acctwork01")
    lapsed = _account("Home", Mailbox(fail=SignInNeeded("signed out")), "accthome01")
    monkeypatch.setattr(connectors, "accounts", lambda conn, klass, **kw: [ok, lapsed])
    assert [m["account"] for m in modreads.reader(None)("mail", "search", {}, None)] == ["Work"]
    monkeypatch.setattr(connectors, "accounts", lambda conn, klass, **kw: [lapsed])
    with pytest.raises(SignInNeeded):
        modreads.reader(None)("mail", "search", {}, None)
