"""Core's write path holds every provider to one exact target.

On 11 October 2026 a cleanup trusted a server's filter and deleted all 96
events of a calendar. Each provider then made its own checks, or none
(a password mailbox's bare EXPUNGE; Google's updates without an etag). The write
path in `core/writes.py` is now the only way core writes through a
provider, and each test here gives it a provider that goes wrong at one
step -- answering for another id, answering with another UID, changing
between the card and the write, deleting more than it was asked -- and
checks that nothing is written, or that what happened is reported.
"""

from __future__ import annotations

import copy
import datetime as dt

import pytest

from core import writes
from core.consent import Consent, Reach, Reversal
from core.monitor import Gate
from steve_sdk import classes
from steve_sdk.errors import AfterWrite, NeedsCard, TargetChanged, Unsupported

DAY = dt.datetime(2026, 10, 14, 9, tzinfo=dt.UTC)


class Calendar(classes.CalendarProvider):
    """An in-memory calendar that can be told to misbehave at one step."""

    def __init__(self, *, conditional: bool = True) -> None:
        self.capabilities = frozenset({classes.CREATE, classes.UPDATE, classes.DELETE}
                                      | ({classes.CONDITIONAL} if conditional else set()))
        self.store: dict[str, classes.Event] = {}
        self.answer_for_other_id = False
        self.answer_with_uid: str | None = None
        self.also_delete: str | None = None
        self.writes = 0
        self._etag = 0

    def add(self, uid: str, title: str) -> str:
        self._etag += 1
        native = f"/cal/{uid}.ics"
        self.store[native] = classes.Event(native_id=native, ident=uid, title=title,
                                           start=DAY, end=DAY + dt.timedelta(hours=1),
                                           condition=f'"{self._etag}"')
        return native

    def events(self, start, end, *, limit):
        return list(self.store.values())

    def fetch(self, native_id):
        if self.answer_for_other_id:
            other = next(n for n in self.store if n != native_id)
            return copy.deepcopy(self.store[other])
        event = self.store.get(native_id)
        if event is not None and self.answer_with_uid:
            event = copy.deepcopy(event)
            event.ident = self.answer_with_uid
        return copy.deepcopy(event)

    def neighbours(self, item):
        return {e.ident: e.condition for e in self.store.values()}

    def create(self, event):
        self.writes += 1
        native = self.add(event.ident, event.title)
        return copy.deepcopy(self.store[native])

    def update(self, event):
        self.writes += 1
        stored = self.store[event.native_id]
        if classes.CONDITIONAL in self.capabilities and event.condition != stored.condition:
            raise TargetChanged("412 Precondition Failed")
        self._etag += 1
        event.condition = f'"{self._etag}"'
        self.store[event.native_id] = copy.deepcopy(event)
        return event

    def delete(self, event):
        self.writes += 1
        del self.store[event.native_id]
        if self.also_delete:
            self.store.pop(self.also_delete, None)


def _two():
    cal = Calendar()
    keep = cal.add("keep@x", "Keep me")
    gone = cal.add("gone@x", "Delete me")
    return cal, keep, gone


def test_a_delete_removes_exactly_the_named_event_and_nothing_else():
    cal, keep, gone = _two()
    writes.delete(cal, gone, expected="gone@x")
    assert list(cal.store) == [keep]


def test_a_server_that_answers_for_another_id_is_refused_before_any_write():
    """The write would land on whatever the server chose."""
    cal, keep, gone = _two()
    cal.answer_for_other_id = True
    with pytest.raises(TargetChanged):
        writes.delete(cal, gone, expected="gone@x")
    assert cal.writes == 0 and len(cal.store) == 2


def test_an_item_whose_uid_is_not_the_one_named_is_refused_before_any_write():
    cal, keep, gone = _two()
    cal.answer_with_uid = "someone-else@x"
    with pytest.raises(TargetChanged):
        writes.delete(cal, gone, expected="gone@x")
    assert cal.writes == 0


def test_a_delete_that_took_more_than_the_one_item_is_reported_as_written():
    """It already happened, so the user must hear of it rather than be told
    one event went -- and not that nothing was written, which TargetChanged
    says, and whose card is asked again: a second yes would write twice."""
    cal, keep, gone = _two()
    cal.also_delete = keep
    with pytest.raises(AfterWrite, match="more changed") as raised:
        writes.delete(cal, gone, expected="gone@x")
    assert not isinstance(raised.value, TargetChanged)


class Unlisted(Calendar):
    """A calendar that cannot list an item's neighbours, and whose server
    answers an update with success and changes nothing."""

    def neighbours(self, item):
        return None

    def update(self, event):
        self.writes += 1
        return event


def test_without_neighbours_an_update_is_still_read_back_as_written():
    """Google's calendar and classic Outlook list no neighbours, nor does an
    MCP server whose listing comes in pages, and the check after a write
    returned at once for them: the one item is read back whatever the
    neighbours, and a change it does not show is reported."""
    cal = Unlisted()
    native = cal.add("a@x", "Lunch")

    def renamed(event):
        event.title = "Lunch at one"
        return event

    with pytest.raises(AfterWrite, match="title not as set"):
        writes.update(cal, native, renamed, expected="a@x")
    assert cal.writes == 1


class ByDay(Calendar):
    """A calendar that lists an item's neighbours by its day, as CalDAV's do."""

    def neighbours(self, item):
        return {e.ident: e.condition for e in self.store.values()
                if e.start.date() == item.start.date()}


def test_an_event_moved_to_another_day_is_checked_on_both_days_before_and_after():
    """Read around the event as it was before the write and as it is after,
    a move to Friday found Friday's events new, and said more changed than
    the one. Both reads cover the day it was on and the day it goes to."""
    cal = ByDay()
    lunch = cal.add("lunch@x", "Lunch")
    choir = cal.add("choir@x", "Choir")
    cal.store[choir].start = DAY + dt.timedelta(days=4)

    def to_friday(event):
        event.start += dt.timedelta(days=4)
        event.end += dt.timedelta(days=4)
        return event

    moved = writes.update(cal, lunch, to_friday, expected="lunch@x")
    assert moved.start == DAY + dt.timedelta(days=4)


def test_without_conditional_writes_an_update_or_delete_needs_a_card():
    cal = Calendar(conditional=False)
    native = cal.add("a@x", "Lunch")
    with pytest.raises(NeedsCard):
        writes.delete(cal, native)
    with pytest.raises(NeedsCard):
        writes.update(cal, native, lambda e: e)
    assert cal.writes == 0


def test_without_conditional_writes_a_create_or_a_flag_needs_no_card():
    """What is new has no earlier version to lose, and a flag is undone with
    a click: on a server that takes no condition, neither waits for a card,
    or every event made on Outlook.com would ask."""
    cal = Calendar(conditional=False)
    made = writes.create(cal, classes.Event(native_id="", ident="new@x", title="New",
                                            start=DAY, end=DAY + dt.timedelta(hours=1)))
    assert made.ident == "new@x" and cal.writes == 1
    mail, flagged = Mail(), []
    assert classes.CONDITIONAL not in mail.capabilities
    writes.modify(mail, "d1", lambda m: flagged.append(m.native_id) or m)
    assert flagged == ["d1"]


def test_an_item_changed_since_its_card_is_not_written_and_asks_again():
    cal = Calendar(conditional=False)
    native = cal.add("a@x", "Lunch")
    card = cal.store[native].digest()
    cal.store[native].start = DAY + dt.timedelta(days=1)  # moved on another device
    with pytest.raises(TargetChanged, match="asking again"):
        writes.delete(cal, native, card=card)
    assert cal.writes == 0


def test_without_a_condition_only_jevs_judgement_stands_in_for_the_card():
    """Jev judging the card unneeded (core/lostupdate.py) is the one verdict
    that lets an unconditional write go without it. A routine's free verdict
    is about the kind of action, not this item, and a look-alike is nothing."""
    from types import SimpleNamespace

    cal = Calendar(conditional=False)
    native = cal.add("a@x", "Lunch")
    judged = Gate("free", Consent(gated=False, reach=Reach.PRIVATE,
                                  reversal=Reversal.IRREVERSIBLE, unconditional=True),
                  "judged", waived=True)
    for verdict in (_FREE, SimpleNamespace(kind="free", waived=True),
                    Gate("card", judged.consent, waived=True)):
        with pytest.raises(NeedsCard):
            writes.update(cal, native, lambda e: e, verdict=verdict)
        with pytest.raises(NeedsCard):
            writes.delete(cal, native, verdict=verdict)
    assert cal.writes == 0

    writes.update(cal, native, lambda e: e, verdict=judged)
    writes.delete(cal, native, verdict=judged)
    assert not cal.store


def test_with_its_card_an_unconditional_delete_goes_ahead():
    cal = Calendar(conditional=False)
    native = cal.add("a@x", "Lunch")
    writes.delete(cal, native, card=cal.store[native].digest())
    assert not cal.store


def test_an_update_is_written_on_the_condition_just_read_and_read_back():
    cal, keep, gone = _two()

    def later(event):
        event.title = "Moved"
        return event

    written = writes.update(cal, gone, later, expected="gone@x")
    assert written.title == "Moved" and cal.store[keep].title == "Keep me"


def test_a_change_may_not_turn_the_item_into_another():
    cal, keep, gone = _two()

    def swap(event):
        event.native_id = keep
        return event

    with pytest.raises(TargetChanged):
        writes.update(cal, gone, swap)
    assert cal.writes == 0


def test_a_create_is_found_again_by_its_own_uid():
    cal = Calendar()
    made = writes.create(cal, classes.Event(native_id="", ident="new@x", title="New",
                                            start=DAY, end=DAY))
    assert made.ident == "new@x" and len(cal.store) == 1


class Mail(classes.MailProvider):
    capabilities = frozenset({classes.SEND})

    def __init__(self):
        self.drafts = {"d1": classes.Message(native_id="d1", ident="<d1@x>", to="a@x",
                                             subject="Hi", body="Hello", draft=True)}
        self.sent: list[str] = []

    def search(self, query, *, limit, since=None):
        return []

    def fetch(self, native_id, *, body_chars=20_000):
        return copy.deepcopy(self.drafts.get(native_id))

    def make_draft(self, draft):
        raise NotImplementedError

    def send_draft(self, message):
        self.sent.append(message.native_id)
        return message

    def delete(self, message):
        raise NotImplementedError

    def sent_items(self, *, since, limit):
        return []

    def headers(self, native_id):
        return {}


def test_a_draft_is_sent_only_after_its_card_and_only_as_the_card_showed_it():
    """A Cc added after the user approved must not ride along."""
    mail = Mail()
    card = mail.drafts["d1"].digest()
    with pytest.raises(NeedsCard):
        writes.send_draft(mail, "d1")
    mail.drafts["d1"].cc = "eve@x"
    with pytest.raises(TargetChanged):
        writes.send_draft(mail, "d1", card=card)
    assert mail.sent == []
    mail.drafts["d1"].cc = ""
    writes.send_draft(mail, "d1", card=card, expected="<d1@x>")
    assert mail.sent == ["d1"]


def test_an_account_that_cannot_send_is_refused_by_core_before_calling_it():
    mail = Mail()
    mail.capabilities = frozenset()
    with pytest.raises(Unsupported):
        writes.send_draft(mail, "d1", verdict=_FREE)
    assert mail.sent == []


_FREE = Gate("free", Consent(gated=False, reach=Reach.PRIVATE, reversal=Reversal.REVERSIBLE))


def test_a_draft_goes_without_a_card_only_on_the_monitors_verdict_that_it_is_free():
    """Until 4e the caller passed a flag, `without_card=card is None`, so a
    send that reached core with no card at all went. Now only the monitor's
    own "free" verdict lets one through: a card's verdict, a look-alike or
    nothing is refused."""
    mail = Mail()
    lookalike = type("Gate", (), {"kind": "free"})()
    for verdict in (None, Gate("card", _FREE.consent), lookalike, True):
        with pytest.raises(NeedsCard):
            writes.send_draft(mail, "d1", verdict=verdict)
    assert mail.sent == []

    writes.send_draft(mail, "d1", verdict=_FREE)
    assert mail.sent == ["d1"]
