"""Classic Outlook's providers through their classes' contract suites.

Every other provider runs `steve_sdk/testing`'s suites; classic Outlook's
two ran only their own tests, so whether its search is checked on our side
when Outlook's cache answers loosely, or its new mail leaves out what the
user sent, was held by nothing a module's author shares. Here the golden
world's Outlook (`golden_world.py`) is a suite's `World`, and every check
of the mail suite and of a read-only calendar's (`calendar.READS`) runs
through `core.writes` on the clock the golden world pins.

Step 5 kept classic Outlook's drafts and calendar as they were, and two
gaps stopped nine checks, held here by a ratchet until they closed: a
draft took no Cc or Bcc (the suite's every draft has a Bcc), and no
calendar entry could be found by its id, which was a hash. Now a draft
takes both and is read back as Outlook holds it, and an entry's id is its
EntryID and its start, so every check runs whole.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import itertools

import pytest

from core import writes
from modules.outlook_classic import calendar as outlook_calendar
from modules.outlook_classic.provider import OutlookCalendar, OutlookMail
from steve_sdk import exact
from steve_sdk.classes import Draft
from steve_sdk.errors import TargetChanged
from steve_sdk.testing import World, calendar, mail
from tests.fakes import outlook as fake_outlook
from tests.unit import golden_world as golden


class OutlookWorld(World):
    """The golden world's Outlook, seeded and looked at as a suite asks.

    A message is keyed by the EntryID it was first filed under: classic
    Outlook's provider gives a message no ident (no Message-ID is read), so
    the store's own id is the only one it holds, and a suite knows such a
    message by its native id as made. Outlook files a sent copy under an id
    of its own; the key follows it there. An entry is keyed by its
    GlobalAppointmentID."""

    def __init__(self) -> None:
        self.unhandled: list[str] = []
        self.wrote: list[dict] = []
        self.outlook = golden.Outlook(self)
        self._ids = itertools.count(1)

    def seed(self, klass, items):
        return [getattr(self, f"_seed_{klass}")(item) for item in items]

    def _seed_mail(self, item: dict) -> str:
        sent = item.get("role") == "sent"
        name, address = email.utils.parseaddr(item.get("sender", "Cy <cy@else.example>"))
        to = email.utils.parseaddr(item.get("to", fake_outlook.ADDRESS))
        made = golden.OutlookMail(self.outlook, subject=item.get("subject", ""),
                                  sender=(name, address), to=[to],
                                  body=item.get("body", "A few lines."),
                                  when=item.get("date") or golden.MOMENT,
                                  unread=bool(item.get("unread")),
                                  conversation=f"t{item.get('thrid', next(self._ids))}")
        self.outlook.put(self.outlook.SENT if sent else self.outlook.INBOX, made)
        return made.EntryID

    def _seed_calendar(self, item: dict) -> str:
        n = next(self._ids)
        made = golden.Appointment(item["title"], item["start"],
                                  int((item["end"] - item["start"]).total_seconds() // 60),
                                  guests=[("", g) for g in item.get("guests", ())])
        # The fake names an entry by its subject; two alike here are two.
        made.EntryID, made.GlobalAppointmentID = f"00000000APPT{n:08X}", f"040000008200E000{n:08X}"
        if rule := item.get("rrule"):
            assert rule.startswith("FREQ=WEEKLY;COUNT="), f"no such repeat here: {rule}"
            made.repeat(dt.timedelta(days=7), int(rule.rpartition("=")[2]))
        self.outlook.put(self.outlook.CALENDAR, made)
        return outlook_calendar.native_id(made.EntryID, made.StartUTC)

    def mutate(self, klass, native_id, **changes):
        assert klass == "mail", f"no suite mutates {klass} here"
        found = self.outlook.GetItemFromID(native_id)
        if changes.get("body") is not None:
            found.Body = changes["body"]
            found.HTMLBody = f"<html><body><p>{changes['body']}</p></body></html>"
        if changes.get("rebuilt"):
            found.EntryID = self.outlook._entry_id()

    def snapshot(self, klass):
        if klass == "mail":
            found: dict[str, list] = {}
            for folder in self.outlook.folders.values():
                for item in folder.items:
                    if isinstance(item, golden.OutlookMail):
                        found.setdefault(exact.norm(item.made_as), []).append(
                            (folder.Name, item.UnRead, item.Subject, item.Body, item.To,
                             item.CC, item.BCC))
            return found
        if klass == "calendar":
            return {item.GlobalAppointmentID: (item.Subject, item.StartUTC, item.EndUTC)
                    for item in self.outlook.folders[self.outlook.CALENDAR].items}
        raise AssertionError(f"no suite looks at {klass} here")


@pytest.fixture
def world(monkeypatch):
    golden.pin_the_clock(monkeypatch)
    made = OutlookWorld()
    made.refused = fake_outlook.serve(monkeypatch, made.outlook)
    yield made
    assert made.refused == [], f"reached without being listed: {made.refused}"


def _named(checks):
    return pytest.mark.parametrize("check", checks, ids=lambda c: c.__name__)


@_named(mail.CHECKS)
def test_classic_outlook_keeps_the_whole_mail_contract(world, check):
    """Outlook's cache answers a search loosely and is walked, not
    filtered: each read the suite holds is checked on our side here, and
    nothing is sent or thrown away without what the write path asks, a
    draft with a Cc or a Bcc among them."""
    check(mail.MailCase(OutlookMail(fake_outlook.context(world.outlook)), world,
                        writes, fake_outlook.ADDRESS))


@_named(calendar.READS)
def test_classic_outlooks_calendar_keeps_a_read_only_calendars_whole_contract(world, check):
    """Classic Outlook's calendar is read only: it reads a range and a
    series' occurrences as a calendar link must, finds each entry again by
    its id, and refuses every write."""
    check(calendar.CalendarCase(OutlookCalendar(fake_outlook.context(world.outlook)),
                                world, writes, fake_outlook.ADDRESS))


def _recipients(world, native_id: str) -> set[tuple[str, int]]:
    """Outlook's own recipients of the item, each with its type."""
    found = world.outlook.GetItemFromID(native_id).Recipients
    return {(found.Item(i).Address, found.Item(i).Type) for i in range(1, found.Count + 1)}


def test_a_draft_to_a_named_person_goes_on_the_card_made_from_it(world):
    """Outlook resolves what it is given into recipients of its own, and a
    read gives each as a name and an address. The draft made was built from
    the arguments, so a card made from it matched no read of the draft, and
    `core.writes` refused to send it."""
    provider = OutlookMail(fake_outlook.context(world.outlook))
    made = provider.make_draft(Draft(to="Rina Sato <rina.sato@contoso.example>",
                                     subject="DES 203", body="I'm in the Thursday section."))
    went = writes.send_draft(provider, made.native_id, card=made.digest())
    assert went.subject == "DES 203" and world.wrote[-1]["outlook"] == "Send"


def test_an_outlook_draft_changed_after_its_check_is_not_sent(world):
    """Outlook sends an item as it is by then. A Cc added in the moment
    between the write path's check and the send went, though the card
    showed none (the review of steps 5 and 6). The draft is read again in
    the call that sends it, and changed since, nothing goes."""

    class ChangedFirst(OutlookMail):
        def send_draft(self, message):
            world.outlook.GetItemFromID(message.native_id).CC = "dana@example.com"
            return super().send_draft(message)

    provider = ChangedFirst(fake_outlook.context(world.outlook))
    made = provider.make_draft(Draft(to="rina.sato@contoso.example", subject="DES 203",
                                     body="I'm in the Thursday section."))

    with pytest.raises(TargetChanged):
        writes.send_draft(provider, made.native_id, card=made.digest())

    assert [w for w in world.wrote if w.get("outlook") == "Send"] == []
    assert provider.fetch(made.native_id).cc == "dana@example.com", "the user's draft stays"


def test_a_draft_outlook_moved_to_deleted_items_under_its_id_is_reported_deleted(
        world, monkeypatch):
    """Outlook's Delete moves an item to Deleted Items, and in a .pst store
    it keeps its EntryID there, so the draft's id still finds it. Classic
    Outlook reads no Message-ID, so the draft has no identifying field, and
    the write path took "" matching "" for the draft still being there: the
    user heard a delete that went had not, and was asked again, and a second
    yes would delete it from Deleted Items for good. An empty field
    identifies nothing."""
    deleted = golden._Folder("Deleted Items")
    world.outlook.folders[3] = deleted  # olFolderDeletedItems

    def moved(item):
        world.outlook._where(item).items.remove(item)
        deleted.items.append(item)

    monkeypatch.setattr(world.outlook, "delete", moved)
    provider = OutlookMail(fake_outlook.context(world.outlook))
    made = provider.make_draft(Draft(to="rina.sato@contoso.example", subject="Plans",
                                     body="Friday?"))
    assert made.ident == ""

    writes.delete(provider, made.native_id, card=made.digest())

    assert [item.EntryID for item in deleted.items] == [made.native_id]
    assert world.outlook.folders[world.outlook.DRAFTS].items == []


def test_a_drafts_cc_and_bcc_are_outlooks_own_copies_new_or_in_reply(world):
    """Each line the user asked for lands as Outlook's recipients of its
    type, in a new draft and in a reply, and is read back so: the card
    binds the copies, so what it shows must be what Outlook will send."""
    provider = OutlookMail(fake_outlook.context(world.outlook))
    new = provider.make_draft(Draft(to="rina.sato@contoso.example", subject="Plans",
                                    body="Friday?", cc="dana@example.com",
                                    bcc="note@else.example"))
    assert _recipients(world, new.native_id) == {
        ("rina.sato@contoso.example", 1), ("dana@example.com", 2), ("note@else.example", 3)}
    assert (new.to, new.cc, new.bcc) == (
        "rina.sato@contoso.example", "dana@example.com", "note@else.example")

    [asked] = world.seed("mail", [{"subject": "Plans", "sender": "Cy <cy@else.example>"}])
    reply = provider.make_draft(Draft(to="", subject="", body="Yes, Friday.", reply_to=asked,
                                      cc="dana@example.com"))
    assert _recipients(world, reply.native_id) == {("cy@else.example", 1),
                                                   ("dana@example.com", 2)}
    assert reply.cc == "dana@example.com" and reply.bcc == ""


def test_an_id_that_names_no_outlook_entry_finds_none(world):
    """A calendar's `fetch` answers None for an entry that is not there:
    an occurrence the series does not have, an entry deleted, or a mail
    item's id. An error would read as the account failing."""
    provider = OutlookCalendar(fake_outlook.context(world.outlook))
    start = golden.MOMENT + dt.timedelta(days=1)
    [single, series] = world.seed("calendar", [
        {"title": "Dentist", "start": start, "end": start + dt.timedelta(hours=1)},
        {"title": "Rota", "start": start, "end": start + dt.timedelta(hours=1),
         "rrule": "FREQ=WEEKLY;COUNT=2"}])
    [message] = world.seed("mail", [{"subject": "Plans"}])
    assert provider.fetch(single).title == "Dentist"
    assert provider.fetch(series).title == "Rota"
    assert provider.fetch(series.partition("@")[0] + "@20261029T133000Z") is None
    world.outlook.folders[world.outlook.CALENDAR].items.pop(0)
    assert provider.fetch(single) is None
    assert provider.fetch(message) is None
