"""The `ctx` a module's own tests are given answers as Steve does.

In the self-upgrade trial (19 October 2026) three builds of one mail tool
passed tests that gave it a ctx of their own, and failed every real call:
the tests' ctx took a limit of 500 and answered in shapes Steve never
gives. `steve_sdk.testing.context.FakeContext` is built on the table core
checks reads with (`steve_sdk/calls.py`); the gate's broker tests check
the two refuse alike.
"""

from __future__ import annotations

import datetime as dt

import pytest

from steve_sdk import calls
from steve_sdk.classes import Message, Task
from steve_sdk.errors import NotConnected
from steve_sdk.testing.context import FakeContext


def _message(native: str, thread: str = "t-1", **kw) -> Message:
    return Message(native_id=native, ident=f"<{native}@mail.example.com>", thread=thread,
                   sender="Iris Ek <iris@example.com>", subject="Saturday",
                   body="See you at ten.", when=1760000000000, condition="etag-1",
                   extra={"List-Id": "x"}, **kw)


def test_a_limit_steve_refuses_is_refused_in_the_modules_tests_too():
    """The trial's tool asked for 500 unread mails: Steve takes 1 to 100,
    and now its tests say so before its card is ever shown."""
    ctx = FakeContext(mail=[_message("m1")])
    with pytest.raises(ValueError, match="limit: a whole number from 1 to 100"):
        ctx.read("mail", "search", query="is:unread", limit=500)
    with pytest.raises(ValueError, match="no parameter 'label' here"):
        ctx.read("mail", "search", label="inbox")
    with pytest.raises(ValueError, match="mail.fetch needs id"):
        ctx.read("mail", "fetch")
    with pytest.raises(PermissionError):
        ctx.read("mail", "delete", id=ctx.id("mail", "m1"))
    with pytest.raises(PermissionError):
        FakeContext(reads=("calendar",)).read("mail", "search")
    assert [args for _, _, args in ctx.asked] == []


def test_an_item_comes_as_a_plain_dict_with_steves_ids_and_never_the_providers_own():
    """A module is given Steve's ids, the account's name and plain values:
    a test that reads `item.subject` or a Message-ID would pass on a shape
    the module never gets."""
    ctx = FakeContext(mail=[_message("m1")], account_name="Work")
    item, = ctx.read("mail", "search", query="saturday", limit=5)
    assert isinstance(item, dict)
    assert item["id"] == ctx.id("mail", "m1") and item["thread"] == ctx.id("mail", "t-1")
    assert item["account"] == "Work" and item["when"] == 1760000000000
    assert {"native_id", "ident", "condition", "extra"}.isdisjoint(item)
    assert ctx.asked == [("mail", "search", {"query": "saturday", "limit": 5})]


def test_a_read_given_no_limit_gives_what_steve_gives_and_no_more():
    """A module that counts on every unread mail without asking for a
    limit finds twenty in Steve, so it finds twenty in its tests."""
    ctx = FakeContext(mail=[_message(f"m{n}") for n in range(30)])
    assert len(ctx.read("mail", "search")) == calls.DEFAULTS["mail.search"]["limit"] == 20
    assert len(ctx.read("mail", "search", limit=100)) == 30


def test_a_search_gives_no_bodies_and_a_fetch_gives_the_one_message_named():
    ctx = FakeContext(mail=[_message("m1"), _message("m2", unread=True)])
    assert {m["body"] for m in ctx.read("mail", "search")} == {None}
    fetched, = ctx.read("mail", "fetch", id=ctx.id("mail", "m2"))
    assert fetched["unread"] is True and fetched["body"] == "See you at ten."
    assert ctx.read("mail", "fetch", id=ctx.id("mail", "gone")) == []
    with pytest.raises(PermissionError, match="not the id of a mail item"):
        ctx.read("mail", "fetch", id="m2")
    with pytest.raises(PermissionError):
        ctx.read("mail", "fetch", id=ctx.id("tasks", "m2"))


def test_a_thread_and_a_task_list_keep_to_what_their_id_names():
    ctx = FakeContext(mail=[_message("m1"), _message("m2", thread="t-2")],
                      tasks=[Task("k1", list_id="home", title="Rent"),
                             Task("k2", list_id="home", title="Bins", done=True),
                             Task("k3", list_id="work", title="Slides")])
    assert [m["id"] for m in ctx.read("mail", "thread", id=ctx.id("mail", "t-2"))] == \
        [ctx.id("mail", "m2")]
    home = ctx.id("tasks", "home")
    assert [t["title"] for t in ctx.read("tasks", "tasks", list=home)] == ["Rent"]
    assert [t["title"] for t in ctx.read("tasks", "tasks", list=home, open_only=False)] == \
        ["Rent", "Bins"]


def test_a_read_from_an_account_by_another_name_is_not_connected():
    ctx = FakeContext(mail=[_message("m1")], account_name="Work")
    assert len(ctx.read("mail", "search", account="Work")) == 1
    with pytest.raises(NotConnected):
        ctx.read("mail", "search", account="Home")


def test_a_time_is_passed_on_as_steve_passes_it():
    ctx = FakeContext()
    start = dt.datetime(2026, 10, 20, 9, tzinfo=dt.UTC)
    ctx.read("calendar", "events", start=start, end="2026-10-21T09:00:00+00:00")
    assert ctx.asked == [("calendar", "events", {"start": "2026-10-20T09:00:00+00:00",
                                                 "end": "2026-10-21T09:00:00+00:00"})]
    with pytest.raises(ValueError, match="an ISO 8601 time with its offset"):
        ctx.read("calendar", "events", start="2026-10-20T09:00:00", end="2026-10-21")


def test_a_proposal_is_checked_kept_and_answered_as_steve_answers_it():
    """`act` changes nothing; it is a card. A module that read its answer
    as done, or proposed past Steve's limit, would pass a ctx of its own."""
    ctx = FakeContext()
    assert ctx.act("upload_backup", {"numbers": 3}) == calls.PROPOSED
    assert ctx.proposed == [("upload_backup", {"numbers": 3})]
    with pytest.raises(ValueError, match=r"action: \[a-z\]"):
        ctx.act("Upload Backup", {})
    with pytest.raises(ValueError, match="args: an object"):
        ctx.act("upload_backup", ["numbers"])
    for _ in range(calls.MAX_ACTS - 1):
        ctx.act("upload_backup", {})
    with pytest.raises(PermissionError, match="5 actions in one call"):
        ctx.act("upload_backup", {})


def test_a_secret_and_what_the_test_did_not_give_are_refused():
    """In the sandbox a module never holds a secret; its header carries
    `{{secret:NAME}}`. A test that handed the code a key would pass code
    that fails there."""
    ctx = FakeContext()
    with pytest.raises(PermissionError, match=r"\{\{secret:token\}\}"):
        ctx.secret("token")
    with pytest.raises(PermissionError, match="no http"):
        ctx.http.request("GET", "https://api.example.com/")
    with pytest.raises(PermissionError, match="no com"):
        ctx.com.open("Outlook.Application")
    ctx.log("ten numbers")
    assert ctx.logged == ["ten numbers"]
