"""The mail class's contract: what every mail provider must do, against its fake.

One suite for every provider, every engine mode and every template
(05-connectors.md, section 8), run through core's write path, which the
caller passes in (`core.writes`): a module's author runs it against their
own fake without importing core's internals by name here. Each check takes
a `MailCase` and raises AssertionError on a broken contract; a test file
parametrizes over `CHECKS`.

The checks seed messages by role (inbox, sent, drafts), not by folder
name, so they hold whatever the server calls its folders. They are the
cases that went wrong before: a search whose server ignored its filter, a
date outside the range, a delete aimed at one of two drafts alike, a
draft changed after its card, a folder rebuilt under its ids, and a sent
message filed in Sent twice.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from steve_sdk import classes, exact
from steve_sdk.classes import Draft, MailProvider, Message
from steve_sdk.errors import NeedsCard, TargetChanged
from steve_sdk.testing import World, changed


@dataclass
class MailCase:
    provider: MailProvider
    world: World
    writes: Any                       # core.writes, the one write path
    address: str                      # the account's own address
    other: str = "ada@else.example"   # someone who writes to it


def _ago(days: float) -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0) - dt.timedelta(days=days)


def _key(ident: str) -> str:
    return exact.norm(ident)


def _known(message: Message) -> str:
    """The key a world files `message` under: its Message-ID, or where it
    has none yet (a classic Outlook draft, until it is sent), the id it was
    made with."""
    return _key(message.ident or message.native_id)


def _seed(case: MailCase, *items: dict) -> list[str]:
    return case.world.seed("mail", list(items))


def reads_what_matches_newest_first(case: MailCase) -> None:
    _seed(case,
          {"sender": f"Ada <{case.other}>", "subject": "Plot assignments", "date": _ago(2)},
          {"sender": f"Ada <{case.other}>", "subject": "Plot water rota", "date": _ago(1)},
          {"sender": "Bo <bo@else.example>", "subject": "Choir", "date": _ago(1)})
    found = case.provider.search(f"from:{case.other}", limit=10)
    assert [m.subject for m in found] == ["Plot water rota", "Plot assignments"], found
    assert all(m.body is None for m in found), "a search fetches no bodies"
    full = case.provider.fetch(found[0].native_id)
    assert full is not None and full.subject == "Plot water rota" and full.body


def a_server_that_ignores_its_filter_is_checked_here(case: MailCase) -> None:
    _seed(case, {"sender": f"Ada <{case.other}>", "subject": "Seed swap", "date": _ago(1)},
          {"sender": "Bo <bo@else.example>", "subject": "Bake sale", "date": _ago(1)},
          # One word of two: a service's relevance search returns it.
          {"sender": "Bo <bo@else.example>", "subject": "Seed catalogue", "date": _ago(1)})
    found = case.provider.search("seed swap", limit=10)
    assert [m.subject for m in found] == ["Seed swap"], found


def dates_are_checked_whatever_the_server_returned(case: MailCase) -> None:
    _seed(case, {"subject": "Last spring's minutes", "date": _ago(400)},
          {"subject": "This week's minutes", "date": _ago(2)})
    found = case.provider.search("minutes", limit=10, since=_ago(30))
    assert [m.subject for m in found] == ["This week's minutes"], found


def new_mail_is_the_inbox_less_what_was_seen(case: MailCase) -> None:
    ids = _seed(case, {"subject": "One", "date": _ago(0.1)}, {"subject": "Two", "date": _ago(0.1)},
                {"role": "sent", "sender": case.address, "to": case.other,
                 "subject": "Mine", "date": _ago(0.1)})
    asked: list[list[str]] = []

    def seen(natives: list[str]) -> set[str]:
        asked.append(natives)
        return {n for n in natives if case.provider.fetch(n).subject == "One"}

    found = case.provider.arrived(since=_ago(1), limit=20, seen=seen)
    assert [m.subject for m in found] == ["Two"], found
    assert asked and all(isinstance(n, str) for n in asked[0])
    assert ids[2] not in asked[0], "sent mail is not new mail"


def sent_items_come_from_sent_alone_with_bodies(case: MailCase) -> None:
    _seed(case, {"role": "sent", "sender": case.address, "to": case.other,
                 "subject": "Re: plot", "body": "Thanks, Ada.", "date": _ago(1)},
          {"role": "sent", "sender": case.address, "to": "bo@else.example",
           "subject": "Choir", "body": "See you.", "date": _ago(1)},
          {"sender": case.other, "to": case.address, "subject": "Plot", "date": _ago(1)})
    mine = case.provider.sent_items(since=_ago(7), limit=10)
    assert sorted(m.subject for m in mine) == ["Choir", "Re: plot"], mine
    assert all(m.body for m in mine), "sent items carry their bodies"
    to_ada = case.provider.sent_items(since=_ago(7), limit=10, to=case.other)
    assert [m.subject for m in to_ada] == ["Re: plot"]


def a_thread_is_found_from_any_message_in_it(case: MailCase) -> None:
    if not case.provider.has(classes.THREADS):
        return
    first = "<plot-1@else.example>"
    ids = _seed(case, {"subject": "Plot", "message_id": first, "date": _ago(3), "thrid": 4242},
                {"subject": "Re: Plot", "date": _ago(2), "thrid": 4242,
                 "headers": {"In-Reply-To": first, "References": first}},
                {"subject": "Unrelated", "date": _ago(1)})
    # The reply by its own id. Found by a search, the first answer stood for
    # it, and a server that ignored the search's filter answered "Unrelated"
    # first: the check failed for the search, which the checks above hold,
    # and said nothing of finding a thread.
    reply = case.provider.fetch(ids[1])
    assert reply is not None and reply.subject == "Re: Plot", reply
    found = case.provider.thread(reply.thread, limit=10)
    assert [m.subject for m in found] == ["Plot", "Re: Plot"], found


def _draft(case: MailCase, subject: str = "Saturday", body: str = "See you at ten.") -> Message:
    return case.provider.make_draft(Draft(to=case.other, subject=subject, body=body,
                                          bcc="note@else.example"))


def a_draft_goes_after_its_card_and_is_filed_in_sent_once(case: MailCase) -> None:
    made = _draft(case)
    assert made.draft and made.native_id
    went = case.writes.send_draft(case.provider, made.native_id, card=made.digest())
    # A message sent may carry a Message-ID of its own, as a mail client
    # sends a draft (Gmail keeps one message per Message-ID), so the copies
    # are looked for by the id it went with.
    after = case.world.snapshot("mail")[_key(went.ident) or _known(made)]
    folders = [entry[0] for entry in after]
    assert len(folders) == len(set(folders)), f"filed twice: {after}"
    assert case.provider.fetch(made.native_id) is None, "the draft is gone once sent"
    sent = case.provider.sent_items(since=_ago(1), limit=10)
    assert [m.subject for m in sent].count("Saturday") == 1, sent


def nothing_is_sent_without_a_card(case: MailCase) -> None:
    made = _draft(case)
    before = case.world.snapshot("mail")
    try:
        case.writes.send_draft(case.provider, made.native_id)
    except NeedsCard:
        pass
    else:
        raise AssertionError("a draft went without its card")
    assert case.world.snapshot("mail") == before


def a_draft_changed_after_its_card_is_not_sent(case: MailCase) -> None:
    made = _draft(case)
    card = made.digest()
    case.world.mutate("mail", made.native_id, body="Actually, eleven.")
    before = case.world.snapshot("mail")
    try:
        case.writes.send_draft(case.provider, made.native_id, card=card)
    except TargetChanged:
        pass
    else:
        raise AssertionError("a draft changed since its card was sent")
    assert case.world.snapshot("mail") == before


def of_two_drafts_alike_only_the_one_named_is_thrown_away(case: MailCase) -> None:
    one, two = _draft(case), _draft(case)
    before = case.world.snapshot("mail")
    case.writes.delete(case.provider, one.native_id, card=one.digest())
    after = case.world.snapshot("mail")
    assert changed(before, after) == {_known(one)}, changed(before, after)
    assert case.provider.fetch(two.native_id) is not None


def a_delete_without_a_condition_asks_first(case: MailCase) -> None:
    made = _draft(case)
    if case.writes.conditional(case.provider, case.provider.fetch(made.native_id)):
        return
    before = case.world.snapshot("mail")
    try:
        case.writes.delete(case.provider, made.native_id)
    except NeedsCard:
        pass
    else:
        raise AssertionError("an unconditional delete went without a card")
    assert case.world.snapshot("mail") == before


def a_rebuilt_folder_refuses_its_old_ids(case: MailCase) -> None:
    made = _draft(case)
    case.world.mutate("mail", made.native_id, rebuilt=True)
    before = case.world.snapshot("mail")
    try:
        case.writes.delete(case.provider, made.native_id, card=made.digest())
    except TargetChanged:
        pass
    else:
        raise AssertionError("a write went to an id from before the folder was rebuilt")
    assert case.world.snapshot("mail") == before


def marking_read_changes_that_message_alone(case: MailCase) -> None:
    if not case.provider.has(classes.FLAGS):
        return
    ids = _seed(case, {"subject": "Unread one", "unread": True, "date": _ago(1)},
                {"subject": "Unread two", "unread": True, "date": _ago(1)})
    target = case.provider.fetch(ids[0])
    before = case.world.snapshot("mail")
    case.writes.modify(case.provider, ids[0], lambda m: case.provider.modify(m, read=True))
    after = case.world.snapshot("mail")
    assert changed(before, after) == {_key(target.ident)}
    assert case.provider.fetch(ids[0]).unread is False


CHECKS: tuple[Callable[[MailCase], None], ...] = (
    reads_what_matches_newest_first,
    a_server_that_ignores_its_filter_is_checked_here,
    dates_are_checked_whatever_the_server_returned,
    new_mail_is_the_inbox_less_what_was_seen,
    sent_items_come_from_sent_alone_with_bodies,
    a_thread_is_found_from_any_message_in_it,
    a_draft_goes_after_its_card_and_is_filed_in_sent_once,
    nothing_is_sent_without_a_card,
    a_draft_changed_after_its_card_is_not_sent,
    of_two_drafts_alike_only_the_one_named_is_thrown_away,
    a_delete_without_a_condition_asks_first,
    a_rebuilt_folder_refuses_its_old_ids,
    marking_read_changes_that_message_alone,
)
